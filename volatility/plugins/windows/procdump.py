# Volatility
# Copyright (C) 2007,2008 Volatile Systems
# Copyright (c) 2008 Brendan Dolan-Gavitt <bdolangavitt@wesleyan.edu>
#
# Additional Authors:
# Mike Auty <mike.auty@gmail.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at
# your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA
#
import os
import re
import struct

from volatility.plugins.overlays.windows import pe_vtypes
from volatility.plugins.windows import common
from volatility import plugin
from volatility import utils


class PEDump(plugin.Command):
    """Dump a PE binary from memory."""

    __name = "pedump"

    def __init__(self, address_space=None, image_base=None, fd=None, filename=None,
                 **kwargs):
        """Dump a PE binary from memory.

        Args:
          address_space: The address space which contains the PE image.
          image_base: The address of the image base (dos header).
          fd: The output file like object which will be used to write the file
            onto.

          filename: Alternatively a filename can be provided to write the PE
            file to.
        """
        super(PEDump, self).__init__(**kwargs)
        self.address_space = address_space
        self.image_base = image_base
        if fd:
            self.out_fd = fd
            self.filename = "FD <%s>" % fd
        elif filename:
            self.out_fd = open(filename, "w")
            self.filename = filename

        # Get the pe profile.
        self.pe_profile = pe_vtypes.PEProfile()

    def WritePEFile(self, fd, address_space, image_base):
        """Dumps the PE file found into the filelike object.

        Note that this function can be used for any PE file (e.g. executable,
        dll, driver etc). Only a base address need be specified. This makes this
        plugin useful as a routine in other plugins.

        Args:
          fd: A writeable filelike object which must support seeking.
          address_space: The address_space to read from.
          image_base: The offset of the dos file header.
        """
        dos_header = self.pe_profile.Object("_IMAGE_DOS_HEADER", offset=image_base,
                                            vm=address_space)
        image_base = dos_header.obj_offset
        nt_header = dos_header.NTHeader

        # First copy the PE file header, then copy the sections.
        data = dos_header.obj_vm.zread(
            image_base, min(1e6, nt_header.OptionalHeader.SizeOfHeaders))
        if not data: return

        fd.seek(0)
        fd.write(data)

        for section in nt_header.Sections:
            # Force some sensible maximum values here.
            size_of_section = min(10e6, section.SizeOfRawData)
            physical_offset = min(100e6, int(section.PointerToRawData))

            data = section.obj_vm.zread(
                section.VirtualAddress + image_base, size_of_section)

            fd.seek(physical_offset, 0)
            fd.write(data)

    def render(self, outfd):
        outfd.write("Dumping PE File at image_base 0x%X to %s\n" % (
                self.image_base, self.filename))

        self.WritePEFile(self.out_fd, self.address_space, self.image_base)

        outfd.write("Done!\n")


class ProcExeDump(common.WinProcessFilter):
    """Dump a process to an executable file sample"""

    __name = "procdump"

    def __init__(self, dump_dir=None, remap=False, outfd=None, **kwargs):
        """Dump a process from memory into an executable.

        In windows PE files are mapped into memory in sections. Each section is
        mapped into a region within the process virtual memory from a region in
        the executable file:

    File on Disk                 Memory Image
0-> ------------    image base-> ------------
     Header                      Header
    ------------                 ------------
     Section 1
    ------------                 ------------
     Section 2                    Section 1
    ------------                 ------------

                                 ------------
                                  Section 2
                                 ------------

        This plugin simply copies the sections from memory back into the file on
        disk. Its likely that some of the pages in memory are not actually
        memory resident, so we might get invalid page reads. In this case the
        region on disk is null padded. If that happens it will not be possible
        to run the executable, but the executable can still be disassembled and
        analysed statically.

        References:
        http://code.google.com/p/corkami/downloads/detail?name=pe-20110117.pdf

        NOTE: Malware can mess with the headers after loading. The remap option
        allows to remap the sections on the disk file so they do not collide.

        Args:
          dump_dir: Directory in which to dump executable files.

          remap: If set, allows to remap the sections on disk so they do not
            overlap.

          fd: Alternatively, a filelike object can be provided directly.
        """
        super(ProcExeDump, self).__init__(**kwargs)
        self.dump_dir = dump_dir or self.session.dump_dir
        self.fd = outfd
        self.pedump = PEDump(session=self.session)

    def check_dump_dir(self, dump_dir=None):
        if not dump_dir:
            raise plugin.PluginError("Please specify a dump directory.")

        if not os.path.isdir(dump_dir):
            raise plugin.PluginError("%s is not a directory" % self.dump_dir)

    def render(self, outfd):
        """Renders the tasks to disk images, outputting progress as they go"""
        for task in self.filter_processes():
            pid = task.UniqueProcessId

            task_address_space = task.get_process_address_space()
            if not task_address_space:
                outfd.write("Can not get task address space - skipping.")
                continue

            if self.fd:
                self.pedump.WritePEFile(
                    self.fd, task_address_space, task.Peb.ImageBaseAddress)
                outfd.write("*" * 72 + "\n")

                outfd.write("Dumping {0}, pid: {1:6} into user provided fd.\n".format(
                        task.ImageFileName, pid))

            # Create a new file.
            else:
                self.check_dump_dir(self.dump_dir)

                sanitized_image_name = re.sub("[^a-zA-Z0-9-_]", "_",
                                              utils.SmartStr(task.ImageFileName))

                filename = os.path.join(self.dump_dir, u"executable.%s_%s.exe" % (
                        sanitized_image_name, pid))

                outfd.write("*" * 72 + "\n")
                outfd.write("Dumping {0}, pid: {1:6} output: {2}\n".format(
                        task.ImageFileName, pid, filename))

                with open(filename, 'wb') as fd:
                    # The Process Environment Block contains the dos header:
                    self.pedump.WritePEFile(
                        fd, task_address_space, task.Peb.ImageBaseAddress)


class DLLDump(ProcExeDump):
    """Dump DLLs from a process address space"""

    __name = "dlldump"

    def __init__(self, regex=".+", **kwargs):
        """Dumps dlls from processes into files.

        Args:
          regex: A regular expression that is applied to the modules name.
        """
        super(DLLDump, self).__init__(**kwargs)
        self.regex = re.compile(regex)

    def render(self, outfd):
        # Make sure the dump dir is ok.
        self.check_dump_dir(self.dump_dir)

        for task in self.filter_processes():
            task_as = task.get_process_address_space()

            # Skip kernel and invalid processes.
            for module in task.get_load_modules():
                process_offset = task_as.vtop(task.obj_offset)
                if process_offset:

                    # Skip the modules which do not match the regex.
                    if not self.regex.search(utils.SmartUnicode(module.BaseDllName)):
                        continue

                    dump_file = "module.{0}.{1:x}.{2:x}.dll".format(
                        task.UniqueProcessId, process_offset, module.DllBase)

                    outfd.write(
                        "Dumping {0}, Process: {1}, Base: {2:8x} output: {3}\n".format(
                            module.BaseDllName, task.ImageFileName, module.DllBase,
                            dump_file))

                    # Use the procdump module to dump out the binary:
                    with open(os.path.join(self.dump_dir, dump_file), "wb") as fd:
                        self.pedump.WritePEFile(fd, task_as, module.DllBase)

                else:
                    outfd.write("Cannot dump {0}@{1} at {2:8x}\n".format(
                            proc.ImageFileName, module.BaseDllName, module.DllBase))


class ModDump(DLLDump):
    """Dump kernel drivers from kernel space."""

    __name = "moddump"

    address_spaces = None

    def find_space(self, image_base):
        """Search through all process address spaces for a PE file."""
        if self.processes is None:
            self.address_spaces = [self.kernel_address_space]
            for task in self.filter_processes():
                self.address_spaces.append(task.get_process_address_space())

        for address_space in self.address_spaces:
            if address_space.is_valid_address(image_base):
                return address_space

    def render(self, outfd):
        # Make sure the dump dir is ok.
        self.check_dump_dir(self.dump_dir)

        modules_plugin = self.session.plugins.modules(session=self.session)

        for module in modules_plugin.lsmod():
            if self.regex.search(utils.SmartUnicode(module.BaseDllName)):
                address_space = self.find_space(module.DllBase)
                if address_space:
                    dump_file = "driver.{0:x}.sys".format(module.DllBase)
                    outfd.write("Dumping {0}, Base: {1:8x} output: {2}\n".format(
                            module.BaseDllName, module.DllBase, dump_file))

                    with open(os.path.join(self.dump_dir, dump_file), "wb") as fd:
                        self.pedump.WritePEFile(fd, address_space, module.DllBase)



class ProcMemDump(ProcExeDump):
    """Dump a process to an executable memory sample"""

    __name = "procmemdump"

    # Disabled - functionality merged into the procexedump module above.
    __abstract = True

    def replace_header_field(self, sect, header, item, value):
        """Replaces a field in a sector header"""
        field_size = item.size()
        start = item.obj_offset - sect.obj_offset
        end = start + field_size
        newval = struct.pack(item.format_string, int(value))
        result = header[:start] + newval + header[end:]
        return result

    def get_image(self, addr_space, base_addr):
        """Outputs an executable memory image of a process"""
        nt_header = self.get_nt_header(addr_space, base_addr)

        sa = nt_header.OptionalHeader.SectionAlignment
        shs = self.pe_profile.get_obj_size('_IMAGE_SECTION_HEADER')

        yield self.get_code(addr_space, base_addr, nt_header.OptionalHeader.SizeOfImage, 0)

        prevsect = None
        sect_sizes = []
        for sect in nt_header.get_sections(self.unsafe):
            if prevsect is not None:
                sect_sizes.append(sect.VirtualAddress - prevsect.VirtualAddress)
            prevsect = sect
        if prevsect is not None:
            sect_sizes.append(self.round(prevsect.Misc.VirtualSize, sa, up = True))

        counter = 0
        start_addr = nt_header.FileHeader.SizeOfOptionalHeader + (
            nt_header.OptionalHeader.obj_offset - base_addr)

        for sect in nt_header.get_sections(self.unsafe):
            sectheader = addr_space.read(sect.obj_offset, shs)
            # Change the PointerToRawData
            sectheader = self.replace_header_field(
                sect, sectheader, sect.PointerToRawData, sect.VirtualAddress)
            sectheader = self.replace_header_field(
                sect, sectheader, sect.SizeOfRawData, sect_sizes[counter])
            sectheader = self.replace_header_field(
                sect, sectheader, sect.Misc.VirtualSize, sect_sizes[counter])

            yield (start_addr + (counter * shs), sectheader)
            counter += 1

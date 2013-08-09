#!/usr/bin/python
# Copyright (C) 2008-2013 Citrix Ltd.
#
# This program is free software; you can redistribute it and/or modify 
# it under the terms of the GNU Lesser General Public License as published 
# by the Free Software Foundation; version 2.1 only.
#
# This program is distributed in the hope that it will be useful, 
# but WITHOUT ANY WARRANTY; without even the implied warranty of 
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the 
# GNU Lesser General Public License for more details.
#
# FileSR: local-file storage repository

import SR, VDI, SRCommand, FileSR, util
import errno
import os
import xmlrpclib
import xs_errors
import vhdutil
from lock import Lock
import cleanup

CAPABILITIES = ["SR_PROBE","SR_UPDATE", "SR_CACHING",
                "VDI_CREATE","VDI_DELETE","VDI_ATTACH","VDI_DETACH",
                "VDI_UPDATE", "VDI_CLONE","VDI_SNAPSHOT","VDI_RESIZE",
                "VDI_GENERATE_CONFIG",
                "VDI_RESET_ON_BOOT", "ATOMIC_PAUSE"]

CONFIGURATION = [ ['device', 'local device path (required) (e.g. /dev/sda3)'] ]

DRIVER_INFO = {
    'name': 'OCFS VHD',
    'description': 'SR plugin which stores disks as VHD files a OCFS filesystem',
    'vendor': 'Citrix Systems Inc',
    'copyright': '(C) 2008-2013 Citrix Systems Inc',
    'driver_version': '1.0',
    'required_api_version': '1.0',
    'capabilities': CAPABILITIES,
    'configuration': CONFIGURATION
    }

DRIVER_CONFIG = {"ATTACH_FROM_CONFIG_WITH_TAPDISK": True}


# The mountpoint for the directory when performing an sr_probe.  All probes
# are guaranteed to be serialised by xapi, so this single mountpoint is fine.
PROBE_MOUNTPOINT = "probe"
REFLINK_CMD = "/usr/bin/reflink"

class OCFSSR(FileSR.FileSR):
    """OCFS file-based storage repository"""
    def handles(type):
        return type == 'ocfs'
    handles = staticmethod(handles)


    def load(self, sr_uuid):
        if not self.dconf.has_key('device') or not self.dconf['device']:
            raise xs_errors.XenError('ConfigDeviceMissing',)
        self.blockdevice = self.dconf['device']
        if not self._isvalidpathstring(self.blockdevice):
            raise xs_errors.XenError('ConfigDeviceInvalid', \
                    opterr='path is %s' % blockdevice)

        self.isMaster = False
        if self.dconf.has_key('SRmaster') and self.dconf['SRmaster'] == 'true':
            self.isMaster = True

        self.uuid = sr_uuid

        self.ops_exclusive = FileSR.OPS_EXCLUSIVE
        self.lock = Lock(vhdutil.LOCK_TYPE_SR, self.uuid)
        self.sr_vditype = SR.DEFAULT_TAP
        self.driver_config = DRIVER_CONFIG
        self.path = os.path.join(SR.MOUNT_BASE, sr_uuid)

    def mount(self, mountpoint, blockdevice):
        try:
            if not util.ioretry(lambda: util.isdir(mountpoint)):
                util.ioretry(lambda: util.makedirs(mountpoint))
        except util.CommandException:
            raise xs_errors.XenError('SRUnavailable', \
                  opterr='no such directory %s' % mountpoint)

        cmd = ['mount', '-t', 'ocfs2', blockdevice, mountpoint, '-o', 'noatime,data=writeback,nointr,commit=60,coherency=buffered']
        try:
             ret = util.pread(cmd)
        except util.CommandException, inst:
                raise xs_errors.XenError('OCFSMount', \
                      opterr='Failed to mount FS. Errno is %d' % inst.code)

    def attach(self, sr_uuid):
        if not self._checkmount():
            self.mount(self.path, self.blockdevice)
        return super(OCFSSR, self).attach(sr_uuid)


    def probe(self):
        pass

    def detach(self, sr_uuid):
        """Detach the SR: Unmounts and removes the mountpoint"""
        if not self._checkmount():
            return
        util.SMlog("Aborting GC/coalesce")
        cleanup.abort(self.uuid)

        # Change directory to avoid unmount conflicts
        os.chdir(SR.MOUNT_BASE)

        cmd = ['umount', self.path ]
        try:
             ret = util.pread(cmd)
        except util.CommandException, inst:
                raise xs_errors.XenError('OCFSUnMount', \
                      opterr='Failed to umount FS. Errno is %d' % inst.code)

        return super(OCFSSR, self).detach(sr_uuid)
        

    def create(self, sr_uuid, size):
        cmd = ['mkfs', '-t', 'ocfs2', '-b', '4K', '-C', '1M', '-N', '16', '-F', self.blockdevice ]
        try:
            ret = util.pread(cmd)
        except util.CommandException, inst:
            raise xs_errors.XenError('OCFSFilesystem', \
                  opterr='mkfs failed error %d' % inst.code)

    def delete(self, sr_uuid):
        # Do a mount and remove any non-VDI files
        self.attach(sr_uuid)
        super(OCFSSR, self).delete(sr_uuid)
        self.detach(sr_uuid)

    def vdi(self, uuid, loadLocked = False):
        if not loadLocked:
            return OCFSFileVDI(self, uuid)
        return OCFSFileVDI(self, uuid)
    
    def _checkmount(self):
        return util.ioretry(lambda: util.pathexists(self.path)) \
               and util.ioretry(lambda: util.ismount(self.path))

class OCFSFileVDI(FileSR.FileVDI):
    def attach(self, sr_uuid, vdi_uuid):
        if self.sr.srcmd.params.has_key("vdi_ref"):
            try:
                vdi_ref = self.sr.srcmd.params['vdi_ref']
                self.session.xenapi.VDI.remove_from_xenstore_data(vdi_ref, \
                        "vdi-type")
                self.session.xenapi.VDI.remove_from_xenstore_data(vdi_ref, \
                        "storage-type")
                self.session.xenapi.VDI.add_to_xenstore_data(vdi_ref, \
                        "storage-type", "ocfs")
            except:
                util.logException("OCFSFileVDI:attach")
                pass

        return super(OCFSFileVDI, self).attach(sr_uuid, vdi_uuid)

    def generate_config(self, sr_uuid, vdi_uuid):
        util.SMlog("OCFSFileVDI.generate_config")
        if not util.pathexists(self.path):
                raise xs_errors.XenError('VDIUnavailable')
        resp = {}
        resp['device_config'] = self.sr.dconf
        resp['sr_uuid'] = sr_uuid
        resp['vdi_uuid'] = vdi_uuid
        resp['command'] = 'vdi_attach_from_config'
        # Return the 'config' encoded within a normal XMLRPC response so that
        # we can use the regular response/error parsing code.
        config = xmlrpclib.dumps(tuple([resp]), "vdi_attach_from_config")
        return xmlrpclib.dumps((config,), "", True)

    def attach_from_config(self, sr_uuid, vdi_uuid):
        """Used for HA State-file only. Will not just attach the VDI but
        also start a tapdisk on the file"""
        util.SMlog("OCFSFileVDI.attach_from_config")
        try:
            if not util.pathexists(self.sr.path):
                self.sr.attach(sr_uuid)
        except:
            util.logException("OCFSFileVDI.attach_from_config")
            raise xs_errors.XenError('SRUnavailable', \
                        opterr='Unable to attach from config')


if __name__ == '__main__':
    SRCommand.run(OCFSSR, DRIVER_INFO)
else:
    SR.registerSR(OCFSSR)

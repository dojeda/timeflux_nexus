"""timeflux.nodes.nexus: Nexus driver"""

from timeflux.core.node import Node
from timeflux.core.exceptions import WorkerInterrupt
import numpy as np
from time import sleep, time
from threading import Lock
import os
import platform
import struct
from ctypes import CDLL, util, byref, c_char_p, c_void_p, c_double, c_int, \
    c_long, c_float, c_short, c_byte, c_longlong, cast, POINTER, c_uint, \
    WINFUNCTYPE, CFUNCTYPE, Structure, wintypes, c_char, c_bool

CMPFUNC = WINFUNCTYPE(None, c_int, c_int, POINTER(c_float))

class DeviceInfoStruct(Structure):
    _fields_ = [
        ('Name', c_char * 40),
        ('SerialNumber',  c_char * 40), # 0-3 type, 4-5 yy, 6-9 index number from year
        ('Description', c_char * 40),
        ('ConnectionType', c_char * 40),
        ('TypeId', wintypes.DWORD),
        ('NumberOfChannels', wintypes.DWORD),
        ('Authenticated', c_bool)
    ]

class ChannelInfoStruct(Structure):
    _fields_ = [
        ('Name', c_char * 40),
        ('SampleRate', wintypes.DWORD),
        ('TypeId', wintypes.DWORD)
    ]

class Nexus(Node):

    ErrorCodeMessage = [
        'OK',
        'No valid Device',
        'Memory allocation failure (Channel info)',
        'False information from device',
        'Could not start device',
        'Could not start Data collection thread',
        'Could not start the Device with the specified serial number',
        'Could not load the Generic Device driver properly'
    ]

    def __init__(self, sampling_rate=512, search_mode='auto', serial_number=0):

        self.serial_number = int(serial_number)
        self.sampling_rate = sampling_rate
        if search_mode not in ['auto', 'usb', 'bluetooth']:
            raise(ValueError('search_mode must be auto, usb or bluetooth. %s was provided' % search_mode))
        self.search_mode = search_mode

        # Thread lock to access buffer
        self.lock = Lock()
        self._buffer = []

        # Start device
        self.lib = None
        self._load_lib()
        self._init_lib()
        self._connect_device()
        self._query_device()
        self._start_device()

    def terminate(self):
        self.lib.StopGenericDevice()

    def _load_lib(self):
        os_name = platform.system()
        bitness = 8 * struct.calcsize('P')
        if os_name in ['Windows', 'Microsoft']:
            libname = 'GenericDeviceInterfaceDLL.dll' if bitness == 32 else 'GenericDeviceInterfaceDLL_x64.dll'
        else:
            raise WorkerInterrupt('Operating system not compatible')
        libpath = os.path.join(os.path.dirname(__file__), '../libs', libname)
        self.lib = CDLL(libpath)

    def _init_lib(self):
        self.callback = CMPFUNC(self._on_data)
        self.lib.InitGenericDevice.argtypes = [CMPFUNC, c_int, c_longlong]
        self.lib.InitGenericDevice.restype = c_int
        self.lib.GetDeviceInfo.argtypes = [POINTER(DeviceInfoStruct)]
        self.lib.GetDeviceInfo.restype = c_bool
        self.lib.GetChannelInfo.argtypes = [c_int, POINTER(ChannelInfoStruct)]
        self.lib.GetChannelInfo.restype = c_bool
        self.lib.StartGenericDevice.argtypes = [POINTER(wintypes.DWORD)]
        self.lib.StartGenericDevice.restype = c_int
        self.lib.StopGenericDevice.restype = c_int

    def _connect_device(self):
        sm = {'auto': 0, 'usb': 1, 'bluetooth': 2}
        ret = self.lib.InitGenericDevice(self.callback, sm[self.search_mode], self.serial_number)
        if ret != 0:
            if ret ==- 6:
                auth = self.lib.ShowAuthenticationWindow()
                if auth == 1:
                    raise WorkerInterrupt('Authentication failed')
            else:
                raise WorkerInterrupt(self.ErrorCodeMessage[abs(ret)])

    def _query_device(self):

        # get device info
        self.device_info = DeviceInfoStruct()
        ret = self.lib.GetDeviceInfo(byref(self.device_info))
        if not ret:
            raise WorkerInterrupt('Failed to retrieve device info')

        n_chan = self.device_info.NumberOfChannels
        sn = self.device_info.SerialNumber

        # get channel info
        TypeID_to_unit = ['NA', 'uV', 'uV', 'mV', 'Bit']
        TypeID_to_type = ['NA', 'voltage', 'voltage', 'voltage', 'Binary']
        channel_info = ChannelInfoStruct()
        self.channels = []
        for i in range(n_chan):
            ret = self.lib.GetChannelInfo(i, byref(channel_info))
            ch_name = channel_info.Name.decode('utf-8')
            self.channels.append(ch_name)
            tid = channel_info.TypeId
            #print(ch_name, tid, TypeID_to_unit[tid], TypeID_to_type[tid])

    def _start_device(self):
        fs = wintypes.DWORD(self.sampling_rate)
        ret = self.lib.StartGenericDevice(byref(fs))
        if ret != 0:
            raise WorkerInterrupt(self.ErrorCodeMessage[abs(ret)])

    def _on_data(self, nSamples, nChannels, data):
        """Callback to receive data."""
        with self.lock:
            out = np.zeros((nSamples, nChannels))
            for i in range(nSamples):
                for ch in range(nChannels):
                    index = (i * nChannels) + ch
                    out[i, ch] = data[index]
            self._buffer.append(out)

    def update(self):
        with self.lock:
            if self._buffer:
                self.o.set(np.vstack(self._buffer), None, self.channels)
                self._buffer = []

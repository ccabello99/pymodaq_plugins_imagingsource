import logging
from typing import Any, Callable, List, Optional, Tuple, Union

from numpy.typing import NDArray
import imagingcontrol4 as ic4
import numpy as np
from qtpy import QtCore
import json
import os

if not hasattr(QtCore, "pyqtSignal"):
    QtCore.pyqtSignal = QtCore.Signal  # type: ignore

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())


class ImagingSourceCamera:
    """Control a Imaging Source camera in the style of pylablib.

    It wraps an :class:`pylon.InstantCamera` instance.

    :param name: Full name of the device.
    :param callback: Callback method for each grabbed image
    """

    camera: ic4.Grabber
    sink: ic4.QueueSink

    def __init__(self, info: str, callback: Optional[Callable] = None, **kwargs):
        super().__init__(**kwargs)

        # Create camera object
        self.camera = ic4.Grabber()
        self.model_name = info.model_name
        self.device_info = info

        # Default place to look for saved device settings
        self.default_device_state_path = os.path.join(os.path.expanduser('~'), 'Downloads', f'{self.model_name}_settings.bin')

        # Callback setup for image grabbing
        self.listener = Listener()
        self.sink = ic4.QueueSink(self.listener, max_output_buffers=1)

        self._pixel_length: Optional[float] = None
        self.attributes = {}
        self.open()
        if callback is not None:
            self.set_callback(callback=callback)

    def open(self) -> None:
        self.camera.device_open(self.device_info)
        self.get_attributes()
        self.attribute_names = [attr['name'] for attr in self.attributes] + [child['name'] for attr in self.attributes if attr.get('type') == 'group' for child in attr.get('children', [])]

    def set_callback(
        self, callback: Callable[[NDArray], None], replace_all: bool = True
    ) -> None:
        """Setup a callback method for continuous acquisition.

        :param callback: Method to be used in continuous mode. It should accept an array as input.
        :param bool replace_all: Whether to remove all previously set callback methods.
        """
        if replace_all:
            try:
                self.listener.signals.data_ready.disconnect()
            except TypeError:
                pass  # not connected
        self.listener.signals.data_ready.connect(callback)
    
    def get_attributes(self):
        """Get the attributes of the camera and store them in a dictionary."""
        model_name = self.model_name.replace(" ", "-")
        file_path = os.path.join(os.environ.get('PROGRAMDATA'), '.pymodaq', f'config_{model_name}.json')
        with open(file_path, 'r') as file:
            attributes = json.load(file)
            self.attributes = self.clean_device_attributes(attributes)

    def get_roi(self) -> Tuple[float, float, float, float, int, int]:
        """Return x0, width, y0, height, xbin, ybin."""
        x0 = self.camera.device_property_map.get_value_int('OffsetX')
        width = self.camera.device_property_map.get_value_int('Width')
        y0 = self.camera.device_property_map.get_value_int('OffsetY')
        height = self.camera.device_property_map.get_value_int('Height')
        xbin = self.camera.device_property_map.get_value_int('BinningHorizontal')
        ybin = self.camera.device_property_map.get_value_int('BinningVertical')
        return x0, x0 + width, y0, y0 + height, xbin, ybin

    def set_roi(
        self, hstart: int, hend: int, vstart: int, vend: int, hbin: int, vbin: int
    ) -> None:
        m_width, m_height = self.get_detector_size()
        inc = self.camera.device_property_map['Width'].increment  # minimum step size
        hstart = detector_clamp(hstart, m_width) // inc * inc
        vstart = detector_clamp(vstart, m_height) // inc * inc
        self.camera.device_property_map.try_set_value('Width', int((detector_clamp(hend, m_width) - hstart) // inc * inc))
        self.camera.device_property_map.try_set_value('Height', int((detector_clamp(vend, m_height) - vstart) // inc * inc))
        self.camera.device_property_map.try_set_value('BinningHorizontal', int(hbin))
        self.camera.device_property_map.try_set_value('BinningVertical', int(vbin))

    def get_detector_size(self) -> Tuple[int, int]:
        """Return width and height of detector in pixels."""
        return self.camera.device_property_map.get_value_int('WidthMax'), self.camera.device_property_map.get_value_int('HeightMax')

    def setup_acquisition(self) -> None:
        self.camera.stream_setup(self.sink, setup_option=ic4.StreamSetupOption.DEFER_ACQUISITION_START)

    def close(self) -> None:
        try:
            if self.camera.is_acquisition_active:
                self.camera.acquisition_stop()
        except ic4.IC4Exception:
            pass

        try:
            if self.camera.is_streaming:
                self.camera.stream_stop()
        except ic4.IC4Exception:
            pass

        try:
            self.camera.device_close()
        except ic4.IC4Exception:
            pass

        self._pixel_length = None

    def save_device_state(self):
        save_path = self.default_device_state_path
        try:
            self.camera.device_save_state_to_file(save_path)
            print(f"Device state saved to {save_path}")
        except ic4.IC4Exception as e:
            print(f"Failed to save device state: {e}")

    def load_device_state(self, load_path):
        if os.path.isfile(load_path):
            try:
                self.camera.device_load_state_from_file(load_path)
                print(f"Device state loaded from {load_path}")
            except ic4.IC4Exception as e:
                print(f"Failed to load device state: {e}")
        else:
            print("No saved settings file found to load.")

    def start_grabbing(self, frame_rate: int) -> None:
        """Start continuously to grab data.

        Whenever a grab succeeded, the callback defined in :meth:`set_callback` is called.
        """
        try:
            self.camera.device_property_map.set_value(ic4.PropId.ACQUISITION_FRAME_RATE, frame_rate)
        except ic4.IC4Exception:
            pass
        self.camera.acquisition_start()


    def clean_device_attributes(self, attributes):
        clean_params = []

        # Check if attributes is a list or dictionary
        if isinstance(attributes, dict):
            items = attributes.items()
        elif isinstance(attributes, list):
            # If it's a list, we assume each item is a parameter (no keys)
            items = enumerate(attributes)  # Use index for 'key'
        else:
            raise ValueError(f"Unsupported type for attributes: {type(attributes)}")

        for idx, attr in items:
            param = {}

            param['title'] = attr.get('title', '')
            param['name'] = attr.get('name', str(idx))  # use index if name is missing
            param['type'] = attr.get('type', 'str')
            param['value'] = attr.get('value', '')
            param['default'] = attr.get('default', None)
            param['limits'] = attr.get('limits', None)
            param['readonly'] = attr.get('readonly', False)

            if param['type'] == 'group' and 'children' in attr:
                children = attr['children']
                # If children is a dict, convert to a list
                if isinstance(children, dict):
                    children = list(children.values())
                param['children'] = self.clean_device_attributes(children)

            clean_params.append(param)

        return clean_params




class Listener(ic4.QueueSinkListener):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.signals = self.ListenerSignal()
        self.frame_ready = False

    def frames_queued(self, sink: ic4.QueueSink):
        buffer = sink.try_pop_output_buffer()
        if buffer is not None:
            self.frame_ready = True
            frame = buffer.numpy_copy()
            buffer.release()
            self.signals.data_ready.emit(frame)
            

    def sink_connected(self, sink: ic4.QueueSink, image_type: ic4.ImageType, min_buffers_required: int) -> bool:
        return True

    def sink_disconnected(self, sink: ic4.QueueSink):
        pass

    class ListenerSignal(QtCore.QObject):
        data_ready = QtCore.pyqtSignal(object)


def detector_clamp(value: Union[float, int], max_value: int) -> int:
    """Clamp a value to possible detector position."""
    return max(0, min(int(value), max_value))
import logging
from typing import Any, Callable, List, Optional, Tuple, Union

from numpy.typing import NDArray
import imagingcontrol4 as ic4
import numpy as np
from qtpy import QtCore, QtWidgets
import json
import os
import time
import math
import platform
import threading
import sys
import ctypes
from ctypes import wintypes

if not hasattr(QtCore, "pyqtSignal"):
    QtCore.pyqtSignal = QtCore.Signal  # type: ignore

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

if sys.platform.startswith("win"):
    import ctypes
    MB_YESNO = 0x04
    MB_ICONQUESTION = 0x20
    MB_OK = 0x00
    MB_ICONINFORMATION = 0x40
    MB_ICONERROR = 0x10
    IDYES = 6
    IDNO = 7
    IDOK = 1

    def _win_message_box(title, text, buttons="yesno", icon="info"):
        icon_map = {"info": MB_ICONINFORMATION, "question": MB_ICONQUESTION, "error": MB_ICONERROR}
        flags = icon_map.get(icon, MB_ICONINFORMATION)
        if buttons == "yesno":
            flags |= MB_YESNO
        else:
            flags |= MB_OK
        return ctypes.windll.user32.MessageBoxW(0, text, title, flags)


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
        self._msg_opener = None

        # Default directory for parameter config files
        if platform.system() == 'Windows':
            self.base_dir = os.path.join(os.environ.get('PROGRAMDATA'), '.pymodaq')
        else:
            self.base_dir = '/etc/.pymodaq'        

        # Default place to look for saved device state configuration
        self.default_device_state_path = os.path.join(self.base_dir, f'{self.model_name}_config.pfs')

        # Callback setup for image grabbing
        self.listener = Listener()
        self.sink = ic4.QueueSink(self.listener, max_output_buffers=1)

        self.attributes = {}
        self.open()
        if callback is not None:
            self.set_callback(callback=callback)

    def open(self) -> None:
        self.camera.device_open(self.device_info)
        self.create_default_config_if_not_exists()
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
        name = self.model_name.replace(" ", "-")
        os_ = platform.system()

        file_path = os.path.join(self.base_dir, f'config_{name}_{os_}.json')

        try:        
            with open(file_path, 'r') as file:
                attributes = json.load(file)
                self.attributes = self.clean_device_attributes(attributes)
        except Exception as e:
            logger.error(f"The config file was not found at {file_path}: ", e, " Make sure to add it !")

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
        inc = self.camera.device_property_map['Width'].increment  # step size
        hstart = detector_clamp(hstart, m_width) // inc * inc
        vstart = detector_clamp(vstart, m_height) // inc * inc

        requested_width = detector_clamp(hend, m_width) - hstart
        requested_height = detector_clamp(vend, m_height) - vstart

        valid_widths = self.camera.device_property_map['Width'].valid_value_set
        valid_heights = self.camera.device_property_map['Height'].valid_value_set
        width_to_set = min(valid_widths, key=lambda x: abs(x - requested_width))
        height_to_set = min(valid_heights, key=lambda x: abs(x - requested_height))

        self.camera.device_property_map.try_set_value('OffsetX', 0)
        self.camera.device_property_map.try_set_value('Width', width_to_set)
        self.camera.device_property_map.try_set_value('OffsetX', int(math.ceil(hstart / 2.) * 2))
        self.camera.device_property_map.try_set_value('OffsetY', 0)
        self.camera.device_property_map.try_set_value('Height', height_to_set)
        self.camera.device_property_map.try_set_value('OffsetY', int(math.ceil(vstart / 2.) * 2))

        self.camera.device_property_map.try_set_value('BinningHorizontal', int(hbin))
        self.camera.device_property_map.try_set_value('BinningVertical', int(vbin))

    def get_detector_size(self) -> Tuple[int, int]:
        """Return width and height of detector in pixels."""
        width = 'WidthMax' if platform.system() == 'Windows' else 'SensorWidth'
        height = 'HeightMax' if platform.system() == 'Windows' else 'SensorHeight'
        return self.camera.device_property_map.get_value_int(width), self.camera.device_property_map.get_value_int(height)

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
            logger.info(f"Device state saved to {save_path}")
        except ic4.IC4Exception as e:
            logger.error(f"Failed to save device state: {e}")

    def load_device_state(self, load_path):
        if os.path.isfile(load_path):
            try:
                self.camera.device_load_state_from_file(load_path)
                logger.info(f"Device state loaded from {load_path}")
            except ic4.IC4Exception as e:
                logger.error(f"Failed to load device state: {e}")
        else:
            logger.warning("No saved settings file found to load.")

    def start_grabbing(self, frame_rate: int) -> None:
        """Start continuously to grab data.

        Whenever a grab succeeded, the callback defined in :meth:`set_callback` is called.
        """
        if frame_rate is not None:
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
    
    def check_attribute_names(self):
        found_exposure = None
        found_gain = None

        possible_exposures = ["ExposureTime", "Exposure_Time"]
        for exp in possible_exposures:
            try:
                self.camera.device_property_map[exp]
                found_exposure = exp
                break
            except Exception:
                pass

        possible_exposure_auto = ["ExposureAuto", "Exposure_Auto"]
        for exp_auto in possible_exposure_auto:
            try:
                self.camera.device_property_map[exp_auto]
                found_exposure_auto = exp_auto
                break
            except Exception:
                pass

        possible_gains = ["Gain"]
        for gain in possible_gains:
            try:
                self.camera.device_property_map[gain]
                found_gain = gain
                break
            except Exception:
                pass

        possible_gain_auto = ["GainAuto", "Gain_Auto"]
        for gain_auto in possible_gain_auto:
            try:
                self.camera.device_property_map[gain_auto]
                found_gain_auto = gain_auto
                break
            except Exception:
                pass

        found_exposure = found_exposure or "ExposureTime"
        found_gain = found_gain or "Gain"
        found_exposure_auto = found_exposure_auto or "ExposureAuto"
        found_gain_auto = found_gain_auto or "GainAuto"

        return found_exposure, found_gain, found_exposure_auto, found_gain_auto

    
    def create_default_config_if_not_exists(self):
        model_name = self.model_name.replace(" ", "-")
        config_dir = self.base_dir
        os_ = platform.system()
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, f'config_{model_name}_{os_}.json')
        if os.path.exists(config_path):
            return
        else:
            self._msg_opener = DefaultConfigMsg()
            msg = QtWidgets.QMessageBox()
            msg.setIcon(QtWidgets.QMessageBox.Question)
            msg.setWindowTitle("Missing Config File")
            msg.setText(f"No config file found for camera model '{model_name}'.")
            msg.setInformativeText("Would you like to auto-create a default configuration file?")
            msg.setStandardButtons(QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
            msg.setDefaultButton(QtWidgets.QMessageBox.Yes)
            QtCore.QTimer.singleShot(0, QtWidgets.QApplication.processEvents)
            user_choice = self.safe_exec_messagebox(msg)
            self.handle_user_choice(user_choice, config_path, model_name)

    def handle_user_choice(self, user_choice, config_path, model_name):

        if user_choice == QtWidgets.QMessageBox.Yes:
            # Try to detect valid exposure/gain names
            found_exposure, found_gain, found_exposure_auto, found_gain_auto = self.check_attribute_names()

            # Build basic config
            config_data = {
                "exposure": {
                    "title": "Exposure Settings",
                    "name": "exposure",
                    "type": "group",
                    "children": {
                        "Exposure Auto": {
                            "title": "Exposure Auto",
                            "name": found_exposure_auto,
                            "type": "led_push",
                            "value": False,
                            "default": False
                        },
                        "Exposure Time": {
                            "title": "Exposure Time (ms)",
                            "name": found_exposure,
                            "type": "slide",
                            "value": 100.0,
                            "default": 100.0,
                            "limits": [0.001, 10000.0]
                        }
                    }
                },
                "gain": {
                    "title": "Gain Settings",
                    "name": "gain",
                    "type": "group",
                    "children": {
                        "Gain Auto": {
                            "title": "Gain Auto",
                            "name": found_gain_auto,
                            "type": "led_push",
                            "value": False,
                            "default": False
                        },
                        "Gain": {
                            "title": "Gain Value",
                            "name": found_gain,
                            "type": "slide",
                            "value": 1.0,
                            "default": 1.0,
                            "limits": [0.0, 2.0]
                        }
                    }
                }
            }
            if model_name == "DMK-33GR0134":
                device_info = {
                "device_info": {
                    "title": "Device Info",
                    "name": "device_info", 
                    "type": "group",
                    "children": {
                        "Device Model Name": {"title": "Device Model Name", "name": "DeviceModelName", "type": "str", "value": "", "readonly": True},
                        "Device Serial Number": {"title": "Device Serial Number", "name": "DeviceSerialNumber", "type": "str", "value": "", "readonly": True},
                        "Device Version": {"title": "Device Version", "name": "DeviceVersion", "type": "str", "value": "", "readonly": True},
                        "Device User ID": {"title": "Device User ID", "name": "DeviceUserID", "type": "str", "value": ""}
                        }
                    }
                }
            else:
                device_info = {
                "device_info": {
                    "title": "Device Info",
                    "name": "device_info", 
                    "type": "group",
                    "children": {
                        "Device Model Name": {"title": "Device Model Name", "name": "DeviceModelName", "type": "str", "value": "", "readonly": True},
                        "Device Serial Number": {"title": "Device Serial Number", "name": "DeviceSerialNumber", "type": "str", "value": "", "readonly": True},
                        "Device Version": {"title": "Device Version", "name": "DeviceVersion", "type": "str", "value": "", "readonly": True},
                        }
                    }
                }
            device_info.update(config_data)
            config_data = device_info
                            
            try:
                print(f"Creating default config for {model_name} at {config_path}")
                with open(config_path, "w") as f:
                    json.dump(config_data, f, indent=4)
                msg_info = QtWidgets.QMessageBox()
                msg_info.setIcon(QtWidgets.QMessageBox.Information)
                msg_info.setWindowTitle("Config Created")
                msg_info.setText(f"Default config file created for '{model_name}'.")
                msg_info.setInformativeText(f"Path:\n{config_path}\n\nYou can edit this file to add/remove parameters.")
                self.safe_exec_messagebox(msg_info, buttons="ok")
                
            except Exception as e:
                msg_err = QtWidgets.QMessageBox()
                msg_err.setIcon(QtWidgets.QMessageBox.Critical)
                msg_err.setWindowTitle("Error Creating Config")
                msg_err.setText(f"Failed to write default config file:\n{e}")
                self.safe_exec_messagebox(msg_err, buttons="ok")
        else:
            msg_info = QtWidgets.QMessageBox()
            msg_info.setIcon(QtWidgets.QMessageBox.Information)
            msg_info.setWindowTitle("Config Not Created")
            msg_info.setText(f"You have chosen not to create a default config file for Basler '{model_name}'.")
            msg_info.setInformativeText(f"You will not have access to camera parameters until you have a valid config file.\n\nYou can find examples of config files in the resources directory of this package or reinitialize and create a default.")
            self.safe_exec_messagebox(msg_info, buttons="ok")

    def safe_exec_messagebox(self, msgbox: QtWidgets.QMessageBox, buttons: str = "yesno") -> int:
        result_container = {}
        finished_event = threading.Event()

        def show_dialog():
            try:
                result_container["choice"] = int(msgbox.exec_())
            except Exception:
                result_container["choice"] = int(QtWidgets.QMessageBox.No)
            finally:
                finished_event.set()

        if self._msg_opener is None:
            self._msg_opener = DefaultConfigMsg()

        # Non-GUI thread (Windows only safe path)
        if sys.platform.startswith("win"):
            title = str(msgbox.windowTitle() or "PyMoDAQ")
            text = str(msgbox.text() or "")
            informative = msgbox.informativeText()
            if informative:
                text += "\n\n" + str(informative)

            try:
                icon_type = "info"
                if msgbox.icon() == QtWidgets.QMessageBox.Question:
                    icon_type = "question"
                elif msgbox.icon() == QtWidgets.QMessageBox.Critical:
                    icon_type = "error"

                res = _win_message_box(title, text, buttons=buttons, icon=icon_type)

                if buttons == "yesno":
                    if res == IDYES:
                        return int(QtWidgets.QMessageBox.Yes)
                    return int(QtWidgets.QMessageBox.No)
                else:
                    return int(QtWidgets.QMessageBox.Ok)

            except Exception:
                return int(QtWidgets.QMessageBox.No)
        else:
            QtCore.QMetaObject.invokeMethod(
                self._msg_opener,
                "run_box",
                QtCore.Qt.ConnectionType.AutoConnection,
                QtCore.Q_ARG(object, show_dialog)
            )

            if QtCore.QThread.currentThread() != QtWidgets.QApplication.instance().thread():
                finished_event.wait()
                QtCore.QTimer.singleShot(0, QtWidgets.QApplication.processEvents)
            else:
                while not finished_event.is_set():
                    QtWidgets.QApplication.processEvents(QtCore.QEventLoop.AllEvents, 50)

            return result_container.get("choice", int(QtWidgets.QMessageBox.No))

    
class DefaultConfigMsg(QtCore.QObject):
    def __init__(self):
        super().__init__()
    @QtCore.Slot(object)
    def run_box(self, fn):
        fn()

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
            #timestamp = int(np.round(buffer.meta_data.device_timestamp_ns)) # it seems this is not a useful timestamp as of now. maybe find out how to fix later
            buffer.release()
            self.signals.data_ready.emit({"frame": frame, "timestamp": time.time_ns()})
            

    def sink_connected(self, sink: ic4.QueueSink, image_type: ic4.ImageType, min_buffers_required: int) -> bool:
        return True

    def sink_disconnected(self, sink: ic4.QueueSink):
        pass

    class ListenerSignal(QtCore.QObject):
        data_ready = QtCore.pyqtSignal(object)


def detector_clamp(value: Union[float, int], max_value: int) -> int:
    """Clamp a value to possible detector position."""
    return max(0, min(int(value), max_value))
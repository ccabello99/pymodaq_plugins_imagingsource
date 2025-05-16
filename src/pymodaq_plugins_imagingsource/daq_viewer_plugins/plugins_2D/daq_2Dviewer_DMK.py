import numpy as np
import imagingcontrol4 as ic4
import time
from datetime import datetime
import os
from pathlib import Path
import imageio.v3 as iio

import warnings
import numpy as np
# Suppress only NumPy RuntimeWarnings (bc of crosshair bug)
warnings.filterwarnings("ignore", category=RuntimeWarning, module="numpy")

# Prevents COM initialization errors associated with ic4.Library.init() being called at the top of the class
import pythoncom
pythoncom.CoInitialize()

from pymodaq_data.h5modules.saving import H5SaverLowLevel
from pymodaq_data.h5modules.data_saving import DataToExportSaver
from pymodaq.utils.daq_utils import ThreadCommand
from pymodaq_plugins_imagingsource.hardware.imagingsource import ImagingSourceCamera
from pymodaq.utils.parameter import Parameter
from pymodaq.utils.data import Axis, DataFromPlugins, DataToExport
from pymodaq.control_modules.viewer_utility_classes import main, DAQ_Viewer_base, comon_parameters
from qtpy import QtWidgets, QtCore


class DAQ_2DViewer_DMK(DAQ_Viewer_base):
    """ 
    
    * Tested with DMK 42BUC03/33GR0134 cameras.
    * Tested on PyMoDAQ version >= 5.0.2
    * Tested on Windows 11
    * Installation instructions: For this camera, you need to install the Imaging Source drivers, 
                                specifically "Device Driver for USB Cameras" and/or "Device Driver for GigE Cameras" in legacy software

    """

    live_mode_available = True

    try:
        ic4.Library.init(api_log_level=ic4.LogLevel.INFO, log_targets=ic4.LogTarget.STDERR)
    except RuntimeError:
        pass # Library already initialized

    device_enum = ic4.DeviceEnum()
    devices = device_enum.devices()
    camera_list = [device.model_name for device in devices]
    

    params = comon_parameters + [
        {'title': 'Camera List:', 'name': 'camera_list', 'type': 'list', 'value': '', 'limits': camera_list},
        {'title': 'ROI', 'name': 'roi', 'type': 'group', 'children': [
            {'title': 'Update ROI', 'name': 'update_roi', 'type': 'bool_push', 'value': False, 'default': False},
            {'title': 'Clear ROI+Bin', 'name': 'clear_roi', 'type': 'bool_push', 'value': False, 'default': False},
            {'title': 'Binning', 'name': 'binning', 'type': 'list', 'limits': [1, 2], 'default': 1},
            {'title': 'Image Width', 'name': 'width', 'type': 'int', 'value': 1280, 'readonly': True},
            {'title': 'Image Height', 'name': 'height', 'type': 'int', 'value': 960, 'readonly': True},
        ]}
    ]

    def ini_attributes(self):
        """Initialize attributes"""

        self.controller: None
        self.user_id = None
        self.device_list_token = None

        self.x_axis = None
        self.y_axis = None
        self.axes = None
        self.data_shape = None
        self.save_frame = False

    def init_controller(self) -> ImagingSourceCamera:

        # Init camera with first available camera (will be a model_name at this point)
        self.user_id = self.settings.param('camera_list').value()
        self.emit_status(ThreadCommand('Update_Status', [f"Trying to connect to {self.user_id}", 'log']))
        devices, camera_list = self.get_camera_list(self.device_enum)
        for cam in camera_list:
            if cam == self.user_id:
                device_idx = camera_list.index(self.user_id)
                device_info = devices[device_idx]
                return ImagingSourceCamera(info=device_info, callback=self.emit_data_callback)
        self.emit_status(ThreadCommand('Update_Status', ["Camera not found", 'log']))
        raise ValueError(f"Camera with name {self.user_id} not found anymore.")

    def ini_detector(self, controller=None):
        """Detector communication initialization

        Parameters
        ----------
        controller: (object)
            custom object of a PyMoDAQ plugin (Slave case). None if only one actuator/detector by controller
            (Master case)

        Returns
        -------
        info: str
        initialized: bool
            False if initialization failed otherwise True
        """

        self.ini_detector_init(old_controller=controller,
                            new_controller=self.init_controller())

        # Register device list changed callback
        self.device_list_token = self.device_enum.event_add_device_list_changed(self.get_camera_list)

        # Register device lost event handler
        self.device_lost_token = self.controller.camera.event_add_device_lost(self.camera_lost)

        # Initialize pixel format before starting stream to avoid default RGB types
        self.controller.camera.device_property_map.set_value('PixelFormat', 'Mono8')

        # Update the UI with available and current camera parameters
        self.add_attributes_to_settings()
        self.update_params_ui()
        for param in self.settings.children():
            param.sigValueChanged.emit(param, param.value())
            if param.hasChildren():
                for child in param.children():
                    child.sigValueChanged.emit(child, child.value())

        # Ensure correct pixel format limits
        misc_group = next(attr for attr in self.controller.attributes if attr['name'] == 'misc')
        pixel_format_limits = next(child['limits'] for child in misc_group.get('children', []) if child['name'] == 'PixelFormat')
        self.settings.child('misc', 'PixelFormat').setLimits(pixel_format_limits)

        # Initialize the stream but defer acquisition start until we start grabbing
        self.controller.setup_acquisition()

        self._prepare_view()
        info = "Initialized camera"
        print(f"{self.user_id} camera initialized successfully")
        self.emit_status(ThreadCommand('Update_Status', [f"{self.user_id} camera initialized successfully"]))
        initialized = True
        return info, initialized
    
    def commit_settings(self, param: Parameter):
        """Apply the consequences of a change of value in the detector settings

        Parameters
        ----------
        param: Parameter
            A given parameter (within detector_settings) whose value has been changed by the user
        """
        name = param.name()
        value = param.value()

        if name == "camera_list":
            if self.controller != None:
                self.close()
            self.ini_detector()

        if name == "device_state_save":
            self.controller.camera.device_save_state_to_file(self.controller.default_device_state_path)
            return
        if name == "device_state_load":
            filepath = self.settings.child('device_state', 'device_state_to_load').value()
            self.controller.camera.device_close()
            self.controller.camera.device_open_from_state_file(filepath)
            # Reinitialize what is needed
            self.controller.camera.device_property_map.set_value('PixelFormat', 'Mono8')
            self.controller.setup_acquisition()
            self.update_params_ui()
            return
        if name == 'TriggerSave':
            if value:
                self.save_frame = True
                return
            else:
                self.save_frame = False
                return
        if name == 'PixelFormat':
            if self.controller != None:
                self.controller.close()
            self.controller = self.init_controller()
            self.controller.camera.device_property_map.set_value(name, value)
            self.controller.setup_acquisition()
            print(f"Pixel format is now: {self.controller.camera.device_property_map.get_value_str(name)}. Restart live grab !")
            self.emit_status(ThreadCommand('Update_Status', [f"Pixel format is now: {self.controller.camera.device_property_map.get_value_str(name)}. Restart live grab !"]))
            self._prepare_view()
            return
    
        if name in self.controller.attribute_names:
            # Special cases
            if name == 'ExposureTime':
                value *= 1e3
            if name == "DeviceUserID":
                self.user_id = value
            # All the rest, just do :
            self.controller.camera.device_property_map.set_value(name, value)

        if name == "update_roi":
            if value:  # Switching on ROI

                # We handle ROI and binning separately for clarity
                (old_x, _, old_y, _, xbin, ybin) = self.controller.get_roi()  # Get current binning
                y0, x0 = self.roi_info.origin.coordinates
                height, width = self.roi_info.size.coordinates

                # Values need to be rescaled by binning factor and shifted by current x0,y0 to be correct.
                new_x = (old_x + x0) * xbin
                new_y = (old_y + y0) * xbin
                new_width = width * ybin
                new_height = height * ybin
                
                new_roi = (new_x, new_width, xbin, new_y, new_height, ybin)
                self.update_rois(new_roi)
                param.setValue(False)
                param.sigValueChanged.emit(param, False)
        elif name == 'binning':
            # We handle ROI and binning separately for clarity
            (x0, w, y0, h, *_) = self.controller.get_roi()  # Get current ROI
            xbin = self.settings.child('roi', 'binning').value()
            ybin = self.settings.child('roi', 'binning').value()
            new_roi = (x0, w, xbin, y0, h, ybin)
            self.update_rois(new_roi)
        elif name == "clear_roi":
            if value:  # Switching on ROI
                wdet, hdet = self.controller.get_detector_size()
                self.settings.child('roi', 'binning').setValue(1)

                new_roi = (0, wdet, 1, 0, hdet, 1)
                self.update_rois(new_roi)
                param.setValue(False)
                param.sigValueChanged.emit(param, False)

    
    def _prepare_view(self):
        """Preparing a data viewer by emitting temporary data. Typically, needs to be called whenever the
        ROIs are changed"""

        width = self.controller.camera.device_property_map.get_value_int('Width')
        height = self.controller.camera.device_property_map.get_value_int('Height')

        self.settings.child('roi', 'width').setValue(width)
        self.settings.child('roi', 'height').setValue(height)

        mock_data = np.zeros((width, height))

        self.x_axis = Axis(label='Pixels', data=np.linspace(1, width, width), index=0)

        if width != 1 and height != 1:
            data_shape = 'Data2D'
            self.y_axis = Axis(label='Pixels', data=np.linspace(1, height, height), index=1)
            self.axes = [self.x_axis, self.y_axis]
        else:
            data_shape = 'Data1D'
            self.axes = [self.x_axis]

        if data_shape != self.data_shape:
            self.data_shape = data_shape
            self.dte_signal_temp.emit(
                DataToExport(f'{self.user_id}',
                            data=[DataFromPlugins(name=f'{self.user_id}',
                                                    data=[np.squeeze(mock_data)],
                                                    dim=self.data_shape,
                                                    labels=[f'{self.user_id}_{self.data_shape}'],
                                                    axes=self.axes)]))

            QtWidgets.QApplication.processEvents()

    def update_rois(self, new_roi):
        (new_x, new_width, new_xbinning, new_y, new_height, new_ybinning) = new_roi
        if new_roi != self.controller.get_roi():
            self.controller.set_roi(hstart=new_x,
                                    hend=new_x + new_width,
                                    vstart=new_y,
                                    vend=new_y + new_height,
                                    hbin=new_xbinning,
                                    vbin=new_ybinning)
            self.close()
            self.ini_detector()
            self._prepare_view()
            self.emit_status(ThreadCommand('Update_Status', [f'Changed ROI. Restart live grab now !']))

    def grab_data(self, Naverage: int = 1, live: bool = False, **kwargs) -> None:
        try:
            self._prepare_view()
            if live:
                self.controller.start_grabbing(frame_rate=self.settings.param('AcquisitionFrameRate').value())
            else:
                if not self.controller.camera.is_acquisition_active:
                    self.controller.camera.acquisition_start()
                while not self.controller.listener.frame_ready:
                    pass # do nothing until frame is available
                if self.controller.camera.is_acquisition_active:
                    self.controller.camera.acquisition_stop()
        except Exception as e:
            self.emit_status(ThreadCommand('Update_Status', [str(e), "log"]))

    def emit_data_callback(self, frame) -> None:
        if not self.save_frame:
            dte = DataToExport(f'{self.user_id}', data=[DataFromPlugins(
                name=f'{self.user_id}',
                data=[np.squeeze(frame)],
                dim=self.data_shape,
                labels=[f'{self.user_id}_{self.data_shape}'],
                axes=self.axes)])
        else:
            dte = DataToExport(f'{self.user_id}', data=[DataFromPlugins(
                name=f'{self.user_id}',
                data=[np.squeeze(frame)],
                dim=self.data_shape,
                labels=[f'{self.user_id}_{self.data_shape}'],
                axes=self.axes, do_save=True)])
            timestamp = datetime.now().strftime("%Y-%m-%d")
            index = self.settings.child('trigger', 'TriggerSaveIndex')
            if not self.settings.child('trigger', 'TriggerSaveLocation').value():
                filepath = os.path.join(os.path.expanduser('~'), 'Downloads', f"{timestamp}_{index.value()}.tiff")
            else:
                filepath = os.path.join(filepath, f"{timestamp}_{index.value()}.tiff")
            iio.imwrite(filepath, frame) 
            index.setValue(index.value()+1)
            index.sigValueChanged.emit(index, index.value())

        self.dte_signal.emit(dte)
        self.controller.listener.frame_ready = False

    def stop(self):
        self.controller.camera.acquisition_stop()
        return ''
    
    def close(self):
        """Terminate the communication protocol"""
        self.controller.attributes = None
        try:
            self.device_enum.event_remove_device_list_changed(self.device_list_token)
            self.controller.camera.event_remove_device_lost(self.device_lost_token)
        except ic4.IC4Exception:
            pass
        self.controller.close()

        self.controller = None  # Garbage collect the controller
        self.status.initialized = False
        self.status.controller = None
        self.status.info = ""
        print(f"{self.user_id} communication terminated successfully")
        self.emit_status(ThreadCommand('Update_Status', [f"{self.user_id} communication terminated successfully"]))
    
    def roi_select(self, roi_info, ind_viewer):
        self.roi_info = roi_info
    
    def crosshair(self, crosshair_info, ind_viewer=0):
        sleep_ms = 150
        self.crosshair_info = crosshair_info
        QtCore.QTimer.singleShot(sleep_ms, QtWidgets.QApplication.processEvents)

    def get_camera_list(self, device_enum: ic4.DeviceEnum):
        devices = device_enum.devices()
        camera_list = [device.model_name for device in devices]
        self.settings.param('camera_list').setLimits(camera_list)
        return devices, camera_list
    
    def camera_lost(self, grabber):
        self.close()
        print(f"Lost connection to {self.user_id}")
        self.emit_status(ThreadCommand('Update_Status', [f"Lost connection to {self.user_id}"]))

    def add_attributes_to_settings(self):
        existing_group_names = {child.name() for child in self.settings.children()}

        for attr in self.controller.attributes:
            attr_name = attr['name']
            if attr.get('type') == 'group':
                if attr_name not in existing_group_names:
                    self.settings.addChild(attr)
                else:
                    group_param = self.settings.child(attr_name)

                    existing_children = {child.name(): child for child in group_param.children()}

                    expected_children = attr.get('children', [])
                    for expected in expected_children:
                        expected_name = expected['name']
                        if expected_name not in existing_children:
                            for old_name, old_child in existing_children.items():
                                if old_child.opts.get('title') == expected.get('title') and old_name != expected_name:
                                    self.settings.child(attr_name, old_name).show(False)
                                    break

                            group_param.addChild(expected)
            else:
                if attr_name not in existing_group_names:
                    self.settings.addChild(attr)
    
    def update_params_ui(self):
        device_map = self.controller.camera.device_property_map

        # Common syntax for any camera model
        self.settings.child('device_info','DeviceModelName').setValue(self.controller.model_name)
        self.settings.child('device_info','DeviceSerialNumber').setValue(self.controller.device_info.serial)
        self.settings.child('device_info','DeviceVersion').setValue(self.controller.device_info.version)
        self.settings.child('device_state', 'device_state_to_load').setValue(self.controller.default_device_state_path)

        # Special case
        if 'DeviceUserID' in self.controller.attribute_names:
            try:
                device_user_id = device_map.get_value_str('DeviceUserID')
                self.settings.child('device_info', 'DeviceUserID').setValue(device_user_id)
                self.user_id = device_user_id
            except Exception:
                pass

        for param in self.controller.attributes:
            param_type = param['type']
            param_name = param['name']
            
            # Already handled
            if param_name == "device_info":
                continue

            if param_type == 'group':
                # Recurse over children in groups
                for child in param['children']:
                    child_name = child['name']
                    child_type = child['type']

                    # Special case: skip these
                    if child_name == 'TriggerSave':
                        continue
                    if child_name == 'TriggerSaveLocation':
                        continue

                    try:
                        if child_type in ['float', 'slide']:
                            value = device_map.get_value_float(child_name)
                        elif child_type == 'int':
                            value = device_map.get_value_int(child_name)
                        elif child_type == 'led_push':
                            value = device_map.get_value_bool(child_name)
                        elif child_type == 'str':
                            value = device_map.get_value_str(child_name)                            
                        else:
                            continue  # Unsupported type, skip

                        # Special case: if parameter is ExposureTime, convert to ms from us
                        if child_name == 'ExposureTime':
                            value *= 1e-3

                        # Set the value
                        self.settings.child(param_name, child_name).setValue(value)

                        # Set limits if defined
                        if 'limits' in child and child_type in ['float', 'slide', 'int'] and not child.get('readonly', False):
                            try:
                                min_limit = device_map[child_name].minimum
                                max_limit = device_map[child_name].maximum

                                if child_name == 'ExposureTime':
                                    min_limit *= 1e-3
                                    max_limit *= 1e-3

                                self.settings.child(param_name, child_name).setLimits([min_limit, max_limit])
                            except ic4.IC4Exception:
                                pass

                    except ic4.IC4Exception:
                        pass
            else:

                try:
                    if param_type in ['float', 'slide']:
                        value = device_map.get_value_float(param_name)
                    elif param_type == 'int':
                        value = device_map.get_value_int(param_name)
                    elif param_type == 'led_push':
                        value = device_map.get_value_bool(param_name)
                    else:
                        return  # Unsupported type, skip

                    # Special case: if parameter is ExposureTime, convert to ms from us
                    if param_name == 'ExposureTime':
                        value *= 1e-3

                    # Set the value
                    self.settings.param(param_name).setValue(value)

                    if 'limits' in param and param_type in ['float', 'slide', 'int'] and not param.get('readonly', False):
                        try:
                            min_limit = device_map[param_name].minimum
                            max_limit = device_map[param_name].maximum

                            if param_name == 'ExposureTime':
                                min_limit *= 1e-3
                                max_limit *= 1e-3

                            self.settings.param(param_name).setLimits([min_limit, max_limit])

                        except ic4.IC4Exception:
                            pass

                except ic4.IC4Exception:
                    pass

if __name__ == '__main__':
    try:
        main(__file__, init=False)
    finally:
        ic4.Library.exit()
import numpy as np
import imagingcontrol4 as ic4
import os
import imageio as iio
import h5py
import json
from uuid6 import uuid7
import platform

import warnings
import numpy as np
# Suppress only NumPy RuntimeWarnings (bc of crosshair bug)
warnings.filterwarnings("ignore", category=RuntimeWarning, module="numpy")

# Prevents COM initialization errors associated with ic4.Library.init() being called at the top of the class
if platform.system() == 'Windows':
    import pythoncom
    pythoncom.CoInitialize()

from pymodaq.utils.daq_utils import ThreadCommand
from pymodaq_plugins_imagingsource.hardware.imagingsource import ImagingSourceCamera
from pymodaq_plugins_imagingsource.resources.extended_publisher import ExtendedPublisher
from pymodaq.utils.parameter import Parameter
from pymodaq.utils.data import Axis, DataFromPlugins, DataToExport
from pymodaq.control_modules.viewer_utility_classes import main, DAQ_Viewer_base, comon_parameters
from qtpy import QtWidgets, QtCore
from typing import Optional


class DAQ_2DViewer_ImagingSourceWithLECO(DAQ_Viewer_base):
    """ 
    
    * Tested with DMK 42BUC03/33GR0134 cameras.
    * Tested on PyMoDAQ version >= 5.0.2
    * Tested on Windows 11/ Ubuntu 24 .04
    * Installation instructions: You must install the Imaging Source drivers for your OS and SDK before use

    """

    live_mode_available = True

    try:
        ic4.Library.init(api_log_level=ic4.LogLevel.INFO, log_targets=ic4.LogTarget.STDERR)
    except RuntimeError:
        pass # Library already initialized

    device_enum = ic4.DeviceEnum()
    devices = device_enum.devices()
    camera_list = []

    model_name_counts = {}
    for device in devices:
        model_name = device.model_name
        count = model_name_counts.get(model_name, 0)
        if count == 0:
            camera_list.append(model_name)
        else:
            camera_list.append(f"{model_name}_{count}")
        model_name_counts[model_name] = count + 1

    
    # Default place to store qsettings for this module
    settings_imagingsource = QtCore.QSettings("PyMoDAQ", "ImagingSource")
    

    params = comon_parameters + [
        {'title': 'Camera List:', 'name': 'camera_list', 'type': 'list', 'value': '', 'limits': camera_list},
        {'title': 'ROI', 'name': 'roi', 'type': 'group', 'children': [
            {'title': 'Update ROI', 'name': 'update_roi', 'type': 'bool_push', 'value': False, 'default': False},
            {'title': 'Clear ROI+Bin', 'name': 'clear_roi', 'type': 'bool_push', 'value': False, 'default': False},
            {'title': 'Binning', 'name': 'binning', 'type': 'list', 'limits': [1, 2], 'default': 1},
            {'title': 'Image Width', 'name': 'width', 'type': 'int', 'value': 1280, 'readonly': True},
            {'title': 'Image Height', 'name': 'height', 'type': 'int', 'value': 960, 'readonly': True},
        ]},
        {'title': 'LECO Logging', 'name': 'leco_log', 'type': 'group', 'children': [
            {'title': 'Send Frame Data ?', 'name': 'leco_send', 'type': 'led_push', 'value': False, 'default': False,
                'tip': 'This leads to huge performance drop as of now. Only use for single grabs, not continuous'},
            {'title': 'Publisher Name', 'name': 'publisher_name', 'type': 'str', 'value': ''},
            {'title': 'Proxy Server Address', 'name': 'proxy_address', 'type': 'str', 'value': 'localhost', 'default': 'localhost',
                'tip': 'Either IP or hostname of LECO proxy server'},
            {'title': 'Proxy Server Port', 'name': 'proxy_port', 'type': 'int', 'value': 11100, 'default': 11100},
            {'title': 'Metadata', 'name': 'leco_metadata', 'type': 'str', 'value': '', 'readonly': True},
            {'title': 'Saving Base Path:', 'name': 'leco_basepath', 'type': 'browsepath', 'value': '', 'filetype': False,
                'tip': 'This is the base directory for a file path sent from a remote director in the metadata'},
        ]}
    ]

    def ini_attributes(self):
        """Initialize attributes"""

        self.controller: None
        self.user_id = None
        self.device_list_token = None

        self.data_shape = None
        self.save_frame = False

        # For LECO operation
        self.metadata = None
        self.data_publisher = None
        self.send_frame_leco = False

    def init_controller(self) -> ImagingSourceCamera:

        # Init camera
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

        # Update the UI with available and current camera parameters
        self.add_attributes_to_settings()
        self.update_params_ui()
        for param in self.settings.children():
            if param.name() == 'device_info':
                continue
            param.sigValueChanged.emit(param, param.value())
            if param.hasChildren():
                for child in param.children():
                    child.sigValueChanged.emit(child, child.value())

        # Initialize pixel format before starting stream to avoid default RGB types
        try:
            self.controller.camera.device_property_map.set_value('PixelFormat', self.settings.child('misc', 'PixelFormat').value())
        except Exception:
            pass # This parameter was not included in the config file

        # Initialize the stream but defer acquisition start until we start grabbing
        self.controller.setup_acquisition()

        # Setup data publisher for LECO if data publisher name is set (ideally it should match the LECO actor name)
        publisher_name = self.settings.child('leco_log', 'publisher_name').value()
        proxy_address = self.settings.child('leco_log', 'proxy_address').value()
        proxy_port = self.settings.child('leco_log', 'proxy_port').value()
        if publisher_name == '':
            print("Publisher name is not set ! Set this first and then reinitialize for LECO logging.")
            self.emit_status(ThreadCommand('Update_Status', ["Publisher name is not set ! Set this first and then reinitialize for LECO logging."]))
        else:
            self.data_publisher = ExtendedPublisher(full_name=publisher_name, host=proxy_address, port=proxy_port)
            print(f"Data publisher {publisher_name} initialized for LECO logging")
            self.emit_status(ThreadCommand('Update_Status', [f"Data publisher {publisher_name} initialized for LECO logging"]))

        try:
            base_path = self.settings_imagingsource.value('leco_log/basepath', os.path.join(os.path.expanduser('~'), 'Downloads'))
        except Exception as e:
            print(f"Error finding LECO base path: {e}")
            base_path = ''
        self.settings.child('leco_log', 'leco_basepath').setValue(base_path)

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
            if not self.settings.child('trigger', 'TriggerMode').value():
                print("Trigger mode is not active ! Start triggering first !")
                self.emit_status(ThreadCommand('Update_Status', ["Trigger mode is not active ! Start triggering first !"]))
                param = self.settings.child('trigger', 'TriggerSaveOptions', 'TriggerSave')
                param.setValue(False) # Turn off save on trigger if triggering is off
                param.sigValueChanged.emit(param, False) 
                return
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
        
        if name == 'leco_send':
            if value:
                self.send_frame_leco = True
            else:
                self.send_frame_leco = False
            return
        if name == 'leco_basepath':
            base_path = value
            if not os.path.exists(base_path):
                print(f"LECO saving base path {base_path} does not exist !")
                self.emit_status(ThreadCommand('Update_Status', [f"LECO saving base path {base_path} does not exist !"]))
            else:
                try:
                    self.settings_imagingsource.setValue('leco_log/basepath', base_path)
                    print(f"LECO saving base path set to {base_path}")
                    self.emit_status(ThreadCommand('Update_Status', [f"LECO saving base path set to {base_path}"]))
                except Exception as e:
                    print(f"Error setting LECO saving base path: {e}")
                    self.emit_status(ThreadCommand('Update_Status', [f"Error setting LECO saving base path: {e}"]))
        if name == 'leco_metadata':
            self.metadata = json.loads(value)
    
        if name in self.controller.attribute_names:
            # Special cases
            if name == 'ExposureTime':
                value *= 1e3
            if name == "DeviceUserID":
                self.user_id = value
            if name == 'TriggerMode':
                if not value:
                    self.save_frame = False
                    param = self.settings.child('trigger', 'TriggerSaveOptions', 'TriggerSave')
                    param.setValue(False) # Turn off save on trigger if we turn off triggering
                    param.sigValueChanged.emit(param, False)
            # we only need to reference these, nothing to do with the cam
            if name == 'TriggerSaveLocation':
                return
            if name == 'TriggerSaveIndex':
                return
            if name == 'Filetype':
                return
            if name == 'Prefix':
                return
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

        mock_data = np.zeros((height, width))

        self.x_axis = Axis(label='Pixels', data=np.linspace(1, width, width), index=1)

        if width != 1 and height != 1:
            data_shape = 'Data2D'
            self.y_axis = Axis(label='Pixels', data=np.linspace(1, height, height), index=0)
            self.axes = [self.y_axis, self.x_axis]
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
            if "Acquisition Frame Rate" in self.controller.attributes:
                frame_rate = self.settings.param('AcquisitionFrameRateAbs').value()
            else:
                frame_rate = None
            if live:
                self.controller.start_grabbing(frame_rate)
            else:
                if not self.controller.camera.is_acquisition_active:
                    self.controller.camera.acquisition_start()
                while not self.controller.listener.frame_ready:
                    pass # do nothing until a frame is ready
                if self.controller.camera.is_acquisition_active:
                    self.controller.camera.acquisition_stop()
        except Exception as e:
            self.emit_status(ThreadCommand('Update_Status', [str(e), "log"]))

    def emit_data_callback(self, frame_data: dict) -> None:
        frame = frame_data['frame']
        timestamp = frame_data['timestamp']
        if frame.ndim == 3 and frame.shape[-1] == 1:
            frame = frame.squeeze(-1)
        shape = frame.shape

        # First emit data to the GUI
        dte = DataToExport(f'{self.user_id}', data=[DataFromPlugins(
            name=f'{self.user_id}',
            data=[np.squeeze(frame)],
            dim=self.data_shape,
            labels=[f'{self.user_id}_{self.data_shape}'],
            axes=self.axes)])
        self.dte_signal.emit(dte)

        # Now, handle data saving with filepath given by user in trigger save settings or from metadata set remotely with LECO
        if self.save_frame:
            self.handle_metadata_and_saving(frame, timestamp, shape)

        # Prepare for next frame
        self.metadata = None
        self.controller.listener.frame_ready = False

    def handle_metadata_and_saving(self, frame, timestamp, shape):
        if not self.settings.child('trigger', 'TriggerMode').value():
            return        
        metadata = self.get_metadata_and_save(frame, timestamp, shape)
        if self.send_frame_leco:
            self.publish_metadata(metadata, frame)
        else:
            self.publish_metadata(metadata)        

    def stop(self):
        self.controller.camera.acquisition_stop()
        return ''
    
    def get_metadata_and_save(self, frame, timestamp, shape):        
        if self.save_frame:
            index = self.settings.child('trigger', 'TriggerSaveOptions', 'TriggerSaveIndex')
            filetype = self.settings.child('trigger', 'TriggerSaveOptions', 'Filetype').value()            
            if self.metadata is not None:
                metadata = self.metadata
                metadata['burst_metadata']['user_id'] = self.user_id
            else:
                metadata = {'burst_metadata':{}, 'file_metadata': {}, 'detector_metadata': {}}
                metadata['burst_metadata']['uuid'] = str(uuid7())
                metadata['burst_metadata']['user_id'] = self.user_id
                metadata['burst_metadata']['timestamp'] = timestamp
            # Include device metadata to send back
            # Account for some uncertainty in timestamp of frame, assume ~100 us for now
            metadata['detector_metadata']['fuzziness'] = 0.1
            count = 0
            for name in self.controller.attribute_names:
                if 'Gain' in name and 'Auto' not in name:
                    metadata['detector_metadata']['gain'] = self.settings.child('gain', name).value()
                    count += 1
                if 'Exposure' in name and 'Auto' not in name:
                    metadata['detector_metadata']['exposure_time'] = self.settings.child('exposure', name).value()
                    count += 1
                if count == 2:
                    break
            metadata['detector_metadata']['shape'] = shape
            if self.metadata is not None:
                metadata = self.metadata
                filepath = self.metadata['file_metadata']['filepath']
                filename = self.metadata['file_metadata']['filename']
                self.metadata['burst_metadata']['user_id'] = self.user_id
                basepath = self.settings.child('leco_log', 'leco_basepath').value()
                filepath = os.path.normpath(os.path.join(basepath, filepath.lstrip(os.path.sep)))
            else:
                filepath = self.settings.child('trigger', 'TriggerSaveOptions', 'TriggerSaveLocation').value()
                prefix = self.settings.child('trigger', 'TriggerSaveOptions', 'Prefix').value()
                if not filepath:
                    filepath = os.path.join(os.path.expanduser('~'), 'Downloads')
                filename = f"{prefix}{index.value()}.{filetype}"
                metadata = {'burst_metadata':{}, 'file_metadata': {}, 'detector_metadata': {}}
                metadata['burst_metadata']['uuid'] = str(uuid7())
                metadata['burst_metadata']['user_id'] = self.user_id
                metadata['burst_metadata']['timestamp'] = timestamp
                metadata['file_metadata']['filepath'] = filepath
                metadata['file_metadata']['filename'] = filename
                index.setValue(index.value()+1)
                index.sigValueChanged.emit(index, index.value())

            metadata['detector_metadata']['fuzziness'] = 0.1 # Account for some uncertainty in timestamp of frame, assume ~100 us for now
            count = 0
            for name in self.controller.attribute_names:
                if 'Gain' in name and 'Auto' not in name:
                    metadata['detector_metadata']['gain'] = self.settings.child('gain', name).value()
                    count += 1
                if 'Exposure' in name and 'Auto' not in name:
                    metadata['detector_metadata']['exposure_time'] = self.settings.child('exposure', name).value()
                    count += 1
                if count == 2:
                    break
            metadata['detector_metadata']['shape'] = shape
            if filetype == 'h5':
                if not filename.endswith('.h5'):
                    filename += '.h5'
                full_path = os.path.join(filepath, filename)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with h5py.File(full_path, 'w') as f:
                    dataset_name = f"frame_{timestamp}"
                    f.create_dataset(dataset_name, data=frame)
                    f.attrs['uuid'] = metadata['burst_metadata']['uuid']
                    f.attrs['user_id'] = metadata['burst_metadata']['user_id']
                    f.attrs['timestamp'] = timestamp
                    f.attrs['exposure_time'] = metadata['detector_metadata']['exposure_time']
                    f.attrs['gain'] = metadata['detector_metadata']['gain']
                    f.attrs['shape'] = metadata['detector_metadata']['shape']
                    f.attrs['fuzziness'] = metadata['detector_metadata']['fuzziness']
                    f.attrs['format_version'] = 'hdf5-v0.1'
            else:
                if not filename.endswith(f".{filetype}"):
                    filename += f".{filetype}"
                if filetype not in ['png', 'jpg', 'jpeg', 'tiff', 'tif']:
                    print(f"Unsupported file type {filetype} for saving frame. Supported types are: png, jpg, jpeg, tiff, tif, h5")
                    self.emit_status(ThreadCommand('Update_Status', [f"Unsupported file type {filetype} for saving frame. Supported types are: png, jpg, jpeg, tiff, tif, h5"]))
                    return
                full_path = os.path.join(filepath, f"{filename}")
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                iio.imwrite(full_path, frame)
        return metadata
    
    def publish_metadata(self, metadata, frame: Optional[np.ndarray] = None):
        if self.data_publisher is not None and self.save_frame:
            if self.send_frame_leco:                        
                self.data_publisher.send_data2({self.settings.child('leco_log', 'publisher_name').value(): 
                                                {'frame': frame, 'metadata': metadata, 
                                                 'message_type': 'detector', 
                                                 'serial_number': self.controller.device_info.serial,
                                                 'format_version': 'hdf5-v0.1'}})
            else:
                self.data_publisher.send_data2({self.settings.child('leco_log', 'publisher_name').value(): 
                                                {'metadata': metadata, 
                                                 'message_type': 'detector',
                                                 'serial_number': self.controller.device_info.serial,
                                                 'format_version': 'hdf5-v0.1'}})
    
    def close(self):
        """Terminate the communication protocol"""
        self.controller.attributes = None
        try:
            self.device_enum.event_remove_device_list_changed(self.device_list_token)
            self.controller.camera.event_remove_device_lost(self.device_lost_token)
        except ic4.IC4Exception:
            pass
        self.controller.close()

        # Just set these to false if camera disconnected for clean GUI
        try:
            param = self.settings.child('trigger', 'TriggerMode')
            param.setValue(False) # Turn off save on trigger if triggering is off
            param.sigValueChanged.emit(param, False)
            param = self.settings.child('trigger', 'TriggerSaveOptions', 'TriggerSave')
            param.setValue(False) # Turn off save on trigger if triggering is off
            param.sigValueChanged.emit(param, False) 
        except Exception:
            pass # no trigger settings

        self.controller = None  # Garbage collect the controller
        self.status.initialized = False
        self.status.controller = None
        self.status.info = ""
        print(f"{self.user_id} communication terminated successfully")
        self.emit_status(ThreadCommand('Update_Status', [f"{self.user_id} communication terminated successfully"]))
    
    def roi_select(self, roi_info, ind_viewer):
        self.roi_info = roi_info
    
    def crosshair(self, crosshair_info, ind_viewer=0):
        self.crosshair_info = crosshair_info
        # Adding a small delay improves performance
        QtCore.QTimer.singleShot(200, QtWidgets.QApplication.processEvents)

    def get_camera_list(self, device_enum: ic4.DeviceEnum):
        devices = device_enum.devices()
        camera_list = []

        model_name_counts = {}
        for device in devices:
            model_name = device.model_name
            count = model_name_counts.get(model_name, 0)
            if count == 0:
                camera_list.append(model_name)
            else:
                camera_list.append(f"{model_name}_{count}")
            model_name_counts[model_name] = count + 1
        param = self.settings.param('camera_list')
        param.setLimits(camera_list)
        param.sigLimitsChanged.emit(param, camera_list)
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

        try:
            self.settings.child('device_state', 'device_state_to_load').setValue(self.controller.default_device_state_path)
        except Exception:
            pass # this parameter was not included in config file

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
                    if child_name == 'TriggerSaveIndex':
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

                        # Special case: if parameter is related to ExposureTime, convert to ms from us
                        if 'Exposure' in child_name and 'Auto' not in child_name:
                            value *= 1e-3

                        # Set the value
                        self.settings.child(param_name, child_name).setValue(value)

                        # Set limits if defined
                        if 'limits' in child and child_type in ['float', 'slide', 'int'] and not child.get('readonly', False):
                            try:
                                min_limit = device_map[child_name].minimum
                                max_limit = device_map[child_name].maximum

                                if 'Exposure' in child_name and 'Auto' not in child_name:
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

                    # Special case: if parameter is related to ExposureTime, convert to ms from us
                    if 'Exposure' in param_name and 'Auto' not in param_name:
                        value *= 1e-3

                    # Set the value
                    self.settings.param(param_name).setValue(value)

                    if 'limits' in param and param_type in ['float', 'slide', 'int'] and not param.get('readonly', False):
                        try:
                            min_limit = device_map[param_name].minimum
                            max_limit = device_map[param_name].maximum

                            if 'Exposure' in param_name and 'Auto' not in param_name:
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
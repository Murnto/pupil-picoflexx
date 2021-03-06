"""
(*)~---------------------------------------------------------------------------
Pupil - eye tracking platform
Copyright (C) 2012-2019 Pupil Labs

Distributed under the terms of the GNU
Lesser General Public License (LGPL v3.0).
See COPYING and COPYING.LESSER for license details.
---------------------------------------------------------------------------~(*)
"""

import logging
import os
import pickle
from time import time
from typing import Optional

from pyglui import ui

import csv_utils
from camera_models import Radial_Dist_Camera, Dummy_Camera
from picoflexx.common import PicoflexxCommon
from picoflexx.royale import RoyaleCameraDevice
from video_capture import manager_classes
from video_capture.base_backend import Base_Manager, Base_Source, Playback_Source
from .frames.depth_data_listener import FramePair

logger = logging.getLogger(__name__)


# noinspection PyPep8Naming
class Picoflexx_Source(PicoflexxCommon, Playback_Source, Base_Source):
    name = "Picoflexx"

    def __init__(
            self,
            g_pool,
            auto_exposure=False,
            record_pointcloud=False,
            current_exposure=2000,
            selected_usecase=None,
            *args,
            **kwargs,
    ):
        super().__init__(g_pool, *args, **kwargs)
        self.camera = RoyaleCameraDevice()

        self.selected_usecase = selected_usecase
        self.frame_count = 0
        self.record_pointcloud = record_pointcloud
        self.royale_timestamp_offset = None

        self._last_frame_time = time()
        """
        Timestamp of the last successful frame.
        """

        self._missed_frame_count = 0
        """
        Number of times get_frames has timed out since the last success.
        """

        self._reconnection_attempts = 0
        """
        Number of times reconnection has been attempted, since connection was
        lost
        """

        self._recording_reconnection_count = 0
        """
        Number of times camera has been reconnected this recording, used to
        name the multiple pointcloud.rrf files, if needed.
        """

        self._ui_exposure = None
        self._ui_usecase = None
        self._switch_record_pointcloud = None
        self.current_exposure = current_exposure
        self._current_exposure_mode = auto_exposure
        self._recording_directory = None

        self.init_device()

    def get_init_dict(self):
        return dict(
            record_pointcloud=self.record_pointcloud,
            auto_exposure=self._current_exposure_mode,
            current_exposure=self.current_exposure,
            selected_usecase=self.selected_usecase,
            **super(Picoflexx_Source, self).get_init_dict(),
        )

    def init_device(self) -> bool:
        if not self.camera.initialize():
            return False

        if not self.camera.is_connected():
            logger.debug("Camera not connected")
            return False

        # Apply settings
        wanted_exposure_mode = self._current_exposure_mode
        # Cache the exposure mode as setting certain use cases will override it
        # e.g. Low Noise Extended

        if self.selected_usecase is not None:
            self.set_usecase(self.selected_usecase)

        self.set_exposure_mode(wanted_exposure_mode)

        if not self._current_exposure_mode and self.current_exposure != 0:
            self.set_exposure(self.current_exposure)
            self.notify_all(
                {"subject": "picoflexx.set_exposure", "delay": 0.3, "exposure": self.current_exposure}
            )

        self.load_camera_state()

        self.on_reconnection()
        self.notify_all({"subject": "picoflexx.reconnected"})

        return True

    def init_ui(self):  # was gui
        self.add_menu()
        self.menu.label = "Pico Flexx"
        self.update_ui()

    def update_ui(self):
        del self.menu[:]

        text = ui.Info_Text("Pico Flexx Options")
        self.menu.append(text)

        if self.online:
            use_cases = self.camera.get_usecases()
            use_cases = [
                use_cases[uc]
                for uc in range(use_cases.size())
                if "MIXED" not in use_cases[uc]
            ]

            self._ui_usecase = ui.Selector(
                "selected_usecase",
                selection=use_cases,
                getter=lambda: self.selected_usecase,
                setter=self.set_usecase,
                label="Activate usecase",
            )
            self.menu.append(self._ui_usecase)

            self._ui_exposure = ui.Slider(
                "current_exposure",
                self,
                min=0,
                max=0,
                setter=self.set_exposure_delayed,
                label="Exposure",
            )
            self.menu.append(self._ui_exposure)

            self.menu.append(
                ui.Switch(
                    "_current_exposure_mode",
                    self,
                    setter=self.set_exposure_mode,
                    label="Auto Exposure",
                )
            )

            self.append_depth_preview_menu()

            self._switch_record_pointcloud = ui.Switch("record_pointcloud", self,
                                                       label="Include 3D pointcloud in recording")
            self.menu.append(self._switch_record_pointcloud)

            self.load_camera_state()
        else:
            text = ui.Info_Text("Pico Flexx needs to be reactivated")
            self.menu.append(text)

    def load_camera_state(self):
        """
        Obtain the current usecase, exposure mode and exposure limits from the
        camera.

        Do nothing if we're not online.
        """

        if not self.online:
            logger.error("Can't get state, not online")
            return

        self.selected_usecase = self.camera.get_current_usecase()
        self._current_exposure_mode = self.get_exposure_mode()
        low, high = self.camera.get_exposure_limits()
        if self.current_exposure > high:
            # Exposure is implicitly clamped to new max
            self.current_exposure = high

        if self._ui_exposure is not None:  # UI is initialized
            # load exposure mode
            self._ui_exposure.read_only = self._current_exposure_mode

            # Update UI with exposure limits of this use case
            self._ui_exposure.minimum = low
            self._ui_exposure.maximum = high

    def deinit_ui(self):
        self.remove_menu()

    def cleanup(self):
        if self.camera is not None:
            self.camera.close()
            self.camera = None

    def on_notify(self, notification):
        if not self.menu:  # we've never been online
            return

        if notification["subject"] == "picoflexx.set_exposure":
            # When the user drags the exposure slider, we set a delayed
            # notification before we actually set it. As the camera freaks out
            # if we ask it to change exposure too often.

            self.set_exposure(notification["exposure"])
        elif notification["subject"] == "recording.started":
            # Disable the "Record RRF" and the Usecase drop down while a
            # recording is in progress.
            if self._switch_record_pointcloud is not None:
                self._switch_record_pointcloud.read_only = True
            if self._ui_usecase is not None:
                self._ui_usecase.read_only = True
            self.frame_count = -1

            self._recording_directory = notification["rec_path"]
            self._recording_reconnection_count = 0

            self.save_recording_metadata(self._recording_directory)

            self.start_pointcloud_recording(self._recording_directory)
        elif notification["subject"] == "recording.stopped":
            # Re-enable the "Record RRF" and the Usecase drop down now that
            # the recording has finished.
            if self._switch_record_pointcloud is not None:
                self._switch_record_pointcloud.read_only = False
            if self._ui_usecase is not None:
                self._ui_usecase.read_only = False

            self.stop_pointcloud_recording()

            self._recording_directory = None

    def on_disconnection(self):
        if self._recording_directory is not None and self.record_pointcloud:
            self.stop_pointcloud_recording()

    def on_reconnection(self):
        if self._recording_directory is not None and self.record_pointcloud:
            self._recording_reconnection_count += 1

            self.start_pointcloud_recording(self._recording_directory)

    def start_pointcloud_recording(self, rec_loc):
        """
        Start an rrf recording if the user has requested it.

        :param rec_loc: Folder of the recording
        """

        if not self.record_pointcloud:
            return

        if self._recording_reconnection_count == 0:
            filename = "pointcloud.rrf"
        else:
            filename = "pointcloud_{}.rrf".format(self._recording_reconnection_count)

        video_path = os.path.join(rec_loc, filename)
        self.camera.start_recording(video_path)

    def stop_pointcloud_recording(self):
        """
        Stop recording an rrf if the user had requested we record one.
        """

        if not self.record_pointcloud:
            return

        self.camera.stop_recording()

    def set_usecase(self, usecase):
        self.camera.set_usecase(usecase)

        self.load_camera_state()

    def set_exposure_delayed(self, exposure):
        # set displayed exposure early, to reduce jankiness while dragging slider
        self.current_exposure = exposure

        self.notify_all(
            {"subject": "picoflexx.set_exposure", "delay": 0.3, "exposure": exposure}
        )

    def set_exposure(self, exposure):
        self.camera.set_exposure(exposure)

    def get_exposure_mode(self):
        return self.camera.get_exposure_mode()

    def set_exposure_mode(self, exposure_mode):
        self._current_exposure_mode = self.camera.set_exposure_mode(exposure_mode)

        if self._ui_exposure is not None:
            self._ui_exposure.read_only = exposure_mode

    def recent_events(self, events):
        frames = self.get_frames()
        if frames:
            events["frame"] = frames.ir
            events["depth_frame"] = frames.depth
            self._recent_frame = frames.ir
            self._recent_depth_frame = frames.depth

            if self._current_exposure_mode:  # auto exposure
                self.current_exposure = frames.depth.exposure_times[1]

    def get_frames(self) -> Optional[FramePair]:
        """
        Obtain the next FramePair, if one is available.

        Adjusting the timestamps of the frames using the timestamp offset.

        :return: The next FramePair
        """

        frames = self.camera.get_frame(block=True, timeout=0.02)
        if frames is None:
            self._missed_frame_count += 1

            if self._missed_frame_count > 45 or time() - self._last_frame_time > 5:
                self.attempt_reconnect()

                # Reset reconnect timers
                self._missed_frame_count = 0
                self._last_frame_time = time()
            return

        self._missed_frame_count = 0
        self._last_frame_time = time()

        if self.royale_timestamp_offset is None:
            # use a constant offset so timestamps from the RRF can be matched
            self.royale_timestamp_offset = self.g_pool.get_timestamp() - time()

        # picoflexx time epoch is unix time, readjust timestamps to pupil time
        frames.ir.timestamp += self.royale_timestamp_offset
        frames.depth.timestamp += self.royale_timestamp_offset

        # To calculate picoflexx camera delay:
        # self.g_pool.get_timestamp() - frames.ir.timestamp
        # Result: ~2-6ms delay depending on selected usecase

        self.frame_count += 1

        return frames

    @property
    def frame_rates(self):
        return 1, self.camera.get_max_frame_rate() if self.online else 30

    @property
    def frame_sizes(self):
        return ((640, 480), (1280, 720), (1920, 1080))

    @property
    def frame_rate(self):
        return self.camera.get_frame_rate() if self.online else 30

    @frame_rate.setter
    def frame_rate(self, new_rate):
        rates = [abs(r - new_rate) for r in self.frame_rates]
        best_rate_idx = rates.index(min(rates))
        rate = self.frame_rates[best_rate_idx]
        if rate != new_rate:
            logger.warning(
                "%sfps capture mode not available at (%s) on 'PicoFlexx Source'. Selected %sfps. "
                % (new_rate, self.frame_size, rate)
            )
        self.camera.set_frame_rate(rate)

    @property
    def jpeg_support(self):
        return False

    @property
    def online(self):
        return self.camera and self.camera.is_connected() and self.camera.is_capturing()

    @property
    def intrinsics(self):
        if not self.online:
            return self._intrinsics or Dummy_Camera(self.frame_size, self.name)

        if self._intrinsics is None or self._intrinsics.resolution != self.frame_size:
            lens_params = self.camera.get_lens_parameters()
            c_x, c_y = lens_params.principal_point
            f_x, f_y = lens_params.focal_length
            p_1, p_2 = lens_params.distortion_tangential
            k_1, k_2, *k_other = lens_params.distortion_radial
            K = [[f_x, 0.0, c_x], [0.0, f_y, c_y], [0.0, 0.0, 1.0]]
            D = k_1, k_2, p_1, p_2, *k_other
            self._intrinsics = Radial_Dist_Camera(K, D, self.frame_size, self.name)

            with open(os.path.join(self.g_pool.user_dir, "picoflexx_intrinsics"), "wb") as f:
                pickle.dump([
                    K, D, self.frame_size, self.name
                ], f)
        return self._intrinsics

    @intrinsics.setter
    def intrinsics(self, model):
        logger.error("Picoflexx backend does not support setting intrinsics manually")

    def save_recording_metadata(self, rec_path):
        meta_info_path = os.path.join(rec_path, "info_picoflexx.csv")

        with open(meta_info_path, "a", newline="") as csvfile:
            csv_utils.write_key_value_file(
                csvfile,
                {
                    "Royale Timestamp Offset": self.royale_timestamp_offset,
                },
                append=True,
            )

    def attempt_reconnect(self):
        logger.debug("attempt_reconnect()")

        if self.camera is None:
            logger.warning("Camera wasn't connected at all?")
            return

        if self._reconnection_attempts == 0:
            self.on_disconnection()
            self.notify_all({"subject": "picoflexx.disconnected"})

        self._reconnection_attempts += 1
        if self.init_device():
            logger.info('Reconnected after {} attempts!'.format(self._reconnection_attempts))
            self._reconnection_attempts = 0


class Picoflexx_Manager(Base_Manager):
    """Simple manager to explicitly activate a fake source"""

    gui_name = "Pico Flexx"

    def __init__(self, g_pool):
        super().__init__(g_pool)

    # Initiates the UI for starting the webcam.
    def init_ui(self):
        self.add_menu()
        from pyglui import ui

        self.menu.append(ui.Info_Text("Backend for https://pmdtec.com/picofamily/"))
        self.menu.append(ui.Button("Activate Pico Flexx", self.activate_source))

    def activate_source(self):
        settings = {}
        settings["name"] = "Picoflexx_Source"
        # if the user set fake capture, we dont want it to auto jump back to the old capture.
        if self.g_pool.process == "world":
            self.notify_all(
                {
                    "subject": "start_plugin",
                    "name": "Picoflexx_Source",
                    "args": settings,
                }
            )
        else:
            logger.warning("Pico Flexx backend is not supported in the eye process.")

    def recent_events(self, events):
        pass

    def get_init_dict(self):
        return {}


manager_classes.append(Picoflexx_Manager)

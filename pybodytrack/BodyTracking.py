import concurrent.futures

import cv2
import time
import threading

import numpy as np
import pandas as pd

from pybodytrack.enums.PoseProcessor import PoseProcessor
from pybodytrack.enums.VideoMode import VideoMode
from pybodytrack.pose_estimators.camera_pose_tracker import CameraPoseTracker
from pybodytrack.pose_estimators.mediapipe_processor import MediaPipeProcessor
from pybodytrack.pose_estimators.yolo_processor import YoloProcessor
from pybodytrack.utils.Message import Message
from pybodytrack.utils.utils import Utils


class BodyTracking:
    def __init__(self, processor="mediapipe", mode=VideoMode.CAMERA, path_video="",custom_model_path="",selected_landmarks=None):
        """
        Initializes the BodyTracking object.

        Parameters:
            processor: An instance of the processor (e.g., YoloProcessor or MediaPipe).
            mode (int): 0 for camera, 1 for video file.
            path_video (str): The path to the video file if mode is 1.
        """
        if processor == PoseProcessor.MEDIAPIPE:
            self.processor = MediaPipeProcessor()
        elif processor == PoseProcessor.YOLO:
            self.processor = YoloProcessor(model_path=custom_model_path)
        #self.processor = processor
        self.tracker = CameraPoseTracker(self.processor,selected_landmarks=selected_landmarks)
        self.mode = mode
        if mode == VideoMode.VIDEO:
            self.path_video = path_video

        # Determine the video source and FPS based on mode
        if self.mode == VideoMode.CAMERA:
            self.cap = cv2.VideoCapture(0)
            self.fps = 30  # Default FPS for camera
        elif self.mode == VideoMode.VIDEO:
            self.cap = cv2.VideoCapture(self.path_video)
            self.fps = self.cap.get(cv2.CAP_PROP_FPS)
            if self.fps == 0:
                self.fps = 30  # Default if unable to determine FPS
        else:
            raise ValueError("Invalid mode selected. Use 0 for camera or 1 for video file.")

        self.frame_interval = 1.0 / self.fps

        # Text identifier for saving CSV (YOLO vs. MediaPipe)
        self.text = "YOLO" if isinstance(self.processor, YoloProcessor) else "MediaPipe"

        # Shared variables and lock for frame processing
        self.latest_frame_lock = threading.Lock()
        self.frame_to_process = None  # Latest frame available for processing
        self.latest_processed_frame = None  # Latest processed frame (with skeleton)
        self.stop_processing = False

        # Processing thread
        self.processing_thread = threading.Thread(target=self._processing_thread_func)
        self.starttime = None
        self.endtime = None

    def set_times(self,startsec,endsec):
        if self.mode == VideoMode.VIDEO:
            if endsec>startsec:
                self.starttime = startsec
                self.endtime = endsec
                if self.starttime is not None:
                    self.cap.set(cv2.CAP_PROP_POS_MSEC, self.starttime* 1000)
            else:
                print("End time must be greater than start time")


    def _processing_thread_func(self):
        """
        Thread function to continuously process frames.
        It always processes the latest frame available.
        """
        while not self.stop_processing:
            with self.latest_frame_lock:
                if self.frame_to_process is not None:
                    frame = self.frame_to_process.copy()
                else:
                    frame = None
            if frame is not None:
                # Process the frame (this should draw the skeleton on the frame)
                self.tracker.process_frame(frame)
                # Store the processed frame for display
                with self.latest_frame_lock:
                    self.latest_processed_frame = frame
            else:
                time.sleep(0.001)  # small delay to avoid busy waiting

    import concurrent.futures

    def start(self, observer=None, fps=None):
        """
        Starts processing and displaying video frames. Instead of computing movement,
        this method sends each new frame's landmark data to the observer. The observer
        is responsible for accumulating a fixed number of frames (e.g. 30) and then
        processing them (such as computing movement or other analysis).

        Parameters:
            observer: An instance of Observer (or subclass) that will receive messages
                      containing the landmark data for each new frame.
            fps: Optional FPS value to use (if None, self.fps is used).
        """
        used_fps = fps if fps is not None else self.fps
        last_index = 0  # To track how many rows from getData() have been sent to the observer

        self.processing_thread.start()

        while self.cap.isOpened():
            loop_start_time = time.time()
            ret, frame = self.cap.read()
            if not ret:
                break

            # Update the frame for processing
            with self.latest_frame_lock:
                self.frame_to_process = frame.copy()
                display_frame = (self.latest_processed_frame.copy()
                                 if self.latest_processed_frame is not None
                                 else frame.copy())

            # Check if an end time was set (for video mode)
            if self.endtime is not None:
                current_msec = self.cap.get(cv2.CAP_PROP_POS_MSEC)
                if current_msec > self.endtime * 1000:
                    break

            # Display the frame
            cv2.imshow("Pose Tracking", display_frame)

            # Retrieve the complete landmark DataFrame so far
            df_all = self.getData()
            if not df_all.empty and len(df_all) > last_index:
                # Get only the new rows that haven't been sent yet
                new_rows = df_all.iloc[last_index:]
                for idx, row in new_rows.iterrows():
                    # Create a message with the landmark data (must include timestamp, x, y, z, etc.)
                    if observer is not None:
                        msg = Message(what=1, obj=row)
                        observer.sendMessage(msg)
                last_index = len(df_all)

            elapsed_time = time.time() - loop_start_time
            remaining_time = self.frame_interval - elapsed_time
            if remaining_time > 0:
                time.sleep(remaining_time)

            # Exit on pressing 'q'
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

        self.stop()

    def startbackup2(self, observer=None, distance_function=None, fps=None):
        """
        Starts processing and displaying video frames. This version accumulates exactly 'fps' frames (non-overlapping blocks).
        When the buffer is full, it computes the movement using the provided distance_function and sends a message to the observer
        containing the movement value along with the start and end timestamps for that block.

        Parameters:
            observer: An instance of Observer (or subclass) with a handleMessage(msg) method.
            distance_function: A callable that takes a Pandas DataFrame and returns a movement value.
            fps: The number of frames to group (if None, self.fps is used).
        """
        used_fps = fps if fps is not None else self.fps
        frame_buffer = []  # List to accumulate new landmark data rows (non-overlapping blocks)
        last_index = 0  # To track already processed rows in the complete DataFrame

        self.processing_thread.start()

        while self.cap.isOpened():
            loop_start_time = time.time()
            ret, frame = self.cap.read()
            if not ret:
                break

            # Update the frame for processing
            with self.latest_frame_lock:
                self.frame_to_process = frame.copy()
                display_frame = (self.latest_processed_frame.copy()
                                 if self.latest_processed_frame is not None
                                 else frame.copy())

            # Check if an end time was set (for video mode)
            if self.endtime is not None:
                current_msec = self.cap.get(cv2.CAP_PROP_POS_MSEC)
                if current_msec > self.endtime * 1000:
                    break

            # Display the frame
            cv2.imshow("Pose Tracking", display_frame)

            # Retrieve the complete landmark DataFrame so far
            df_all = self.getData()
            if not df_all.empty and len(df_all) > last_index:
                # Get only the new rows that haven't been processed yet
                new_rows = df_all.iloc[last_index:]
                for idx, row in new_rows.iterrows():
                    frame_buffer.append(row)
                last_index = len(df_all)  # Update pointer

            # When we have exactly (or more than) 'used_fps' frames, process the first block
            if len(frame_buffer) >= used_fps and distance_function is not None:
                buffer_df = pd.DataFrame(frame_buffer[:used_fps])
                frame_buffer = frame_buffer[used_fps:]  # Remove processed rows
                movement = distance_function(buffer_df)
                start_time = buffer_df.iloc[0]['timestamp']
                end_time = buffer_df.iloc[-1]['timestamp']

                # Send a success message with movement data
                if observer is not None:
                    msg = Message(what=1, obj=(movement, start_time, end_time))
                    observer.sendMessage(msg)

            elapsed_time = time.time() - loop_start_time
            remaining_time = self.frame_interval - elapsed_time
            if remaining_time > 0:
                time.sleep(remaining_time)

            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

        self.stop()

    def startbackup(self):
        """
        Starts the processing thread and the main loop for reading, processing,
        and displaying the video frames.
        """
        self.processing_thread.start()
        while self.cap.isOpened():
            start_time = time.time()
            ret, frame = self.cap.read()
            if not ret:
                break

            # Update the frame for processing
            with self.latest_frame_lock:
                self.frame_to_process = frame.copy()
                # Use the processed frame if available; otherwise, show the raw frame
                if self.latest_processed_frame is not None:
                    display_frame = self.latest_processed_frame.copy()
                else:
                    display_frame = frame.copy()

            if self.endtime is not None:
                current_msec = self.cap.get(cv2.CAP_PROP_POS_MSEC)
                if current_msec > self.endtime * 1000:
                    break
            cv2.imshow("Pose Tracking", display_frame)
            elapsed_time = time.time() - start_time
            remaining_time = self.frame_interval - elapsed_time
            if remaining_time > 0:
                time.sleep(remaining_time)

            # Exit on pressing 'q'
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

        self.stop()

    def stop(self):
        """
        Stops the processing thread, releases the video source,
        and closes all OpenCV windows.
        """
        self.stop_processing = True
        self.processing_thread.join()
        self.cap.release()
        cv2.destroyAllWindows()

    def getData(self):
        return self.tracker.get_dataframe()

    def save_csv(self, filename=None):
        """
        Saves the tracking data to a CSV file.

        Parameters:
            filename (str): Optional filename for the CSV. If None, a default
                            name based on the processor type is used.
        """
        if filename is None:
            filename = "pose_data" + self.text + "_"+str(time.time())+".csv"
        self.tracker.save_to_csv(filename)

    def filter_interval(self,start_sec, end_sec):
        """
        Filters the DataFrame 'df', which contains a 'timestamp' column in Unix epoch format,
        to return rows corresponding to the time interval of the video based on the given offsets in seconds.

        It is assumed that:
          - The first timestamp (df.iloc[0]['timestamp']) corresponds to the beginning of the video.
          - For example, if start_sec=10 and end_sec=30, the function returns data between t0+10 seconds and t0+30 seconds.

        Parameters:
          df (DataFrame): DataFrame containing a 'timestamp' column in Unix epoch format.
          start_sec (int or float): The start second offset (relative to the first timestamp).
          end_sec (int or float): The end second offset (relative to the first timestamp).

        Returns:
          DataFrame: A subset of df filtered according to the specified time interval.
        """
        # Starting timestamp of the video:
        df = self.getData()
        t0 = df.iloc[0]['timestamp']
        # Calculate the Unix timestamps for the desired interval:
        start_ts = t0 + start_sec
        end_ts = t0 + end_sec

        # Filter the DataFrame:
        return df[(df['timestamp'] >= start_ts) & (df['timestamp'] <= end_ts)]


    def movement_per_second(self,total_movement):
        """
        Calculate the average movement per second.

        Assumes the DataFrame has a 'timestamp' column as the first column,
        with timestamps expressed in seconds.

        :param total_movement: Total movement value (from any motion method).
        :param df: Pandas DataFrame containing the landmark data, including 'timestamp'.
        :return: Movement per second.
        """
        # Extract timestamps (assumed to be the first column)
        timestamps = self.getData().iloc[:, 0].values
        duration = timestamps[-1] - timestamps[0]
        if duration <= 0:
            return 0.0
        return total_movement / duration


    def movement_per_frame(self,total_movement):
        """
        Calculate the average movement per frame.

        :param total_movement: Total movement value computed using any motion method.
        :param df: Pandas DataFrame containing the landmark data.
        :return: Average movement per frame.
        """
        n_frames = len(self.getData())
        if n_frames <= 1:
            return 0.0
        return total_movement / (n_frames - 1)

    def stats_summary(self, movement):
        '''
        Average: The mean movement per frame.
        Standard Deviation: How spread out the movement values are around the mean.
        Median: The middle value of the ordered movement values, less sensitive to outliers.
        95th Percentile: The value below which 95% of the frame movement values fall.

        :param movement:
        :return:
        '''
        print("Raw amount of movement:", movement)
        #data = self.getData()  # Guardamos el resultado de self.getData()
        a = self.movement_per_second(movement)
        print("Amount of movement per second:", a)
        a = self.movement_per_frame(movement)
        print("Amount of movement per frame:", a)
        a = self.movement_per_landmark(movement, len(self.tracker.selected_landmarks))
        print("Amount of movement per landmark:", a)
        a = self.normalized_movement_index(movement, len(self.tracker.selected_landmarks))
        print("Normalized amount of movement:", a)

    def normalized_movement_index(self,total_movement, num_landmarks):
        """
        Calculate a normalized movement index by dividing the total movement by both the duration
        (in seconds) and the number of landmarks. This yields a dimensionless index that facilitates
        comparison across videos with different durations or landmark counts.

        Assumes the DataFrame has a 'timestamp' column as the first column.

        :param total_movement: Total movement computed using any motion method.
        :param df: Pandas DataFrame with the landmark data (including 'timestamp').
        :param num_landmarks: Total number of landmarks used in the measurement.
        :return: Normalized movement index.
        """
        timestamps = self.getData().iloc[:, 0].values  # Assuming the first column is timestamp
        duration = timestamps[-1] - timestamps[0]
        if duration <= 0 or num_landmarks <= 0:
            return 0.0
        return total_movement / (duration * num_landmarks)


    def movement_per_landmark(self,total_movement, num_landmarks):
        """
        Calculate the average movement per landmark.

        :param total_movement: Total movement computed using any motion method.
        :param num_landmarks: Total number of landmarks.
        :return: Average movement per landmark.
        """
        if num_landmarks <= 0:
            return 0.0
        return total_movement / num_landmarks
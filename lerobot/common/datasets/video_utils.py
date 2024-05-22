#!/usr/bin/env python

# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
import math
import multiprocessing
import subprocess
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import av
import pyarrow as pa
import rerun as rr
import torch
import torchvision
from datasets.features.features import register_feature
from huggingface_hub.file_download import hf_hub_download


class PeekableIterator:
    """Trivial implementation of a peekable iterator

    Next element can be peeked without consuming it.
    Use next() to consume the element.
    """

    def __init__(self, iterable):
        self.iterator = iter(iterable)
        self.peeked = None

    def __iter__(self):
        return self

    def __next__(self):
        if self.peeked is not None:
            if self.peeked is StopIteration:
                raise StopIteration
            ret = self.peeked
            self.peeked = None
            return ret
        return next(self.iterator)

    def peek(self):
        if not self.peeked:
            try:
                self.peeked = next(self.iterator)
            except StopIteration:
                self.peeked = StopIteration
                return None
        return self.peeked


class SequentialRerunVideoReader:
    """Video reader that returns sequential `rerun.Image` objects from a video file.

    Each call to `next_frame()` returns the next frame in the video file
    within the specified tolerance of the requested video timestamp.

    The stream decoding happens in its own process with a 10-frame read-ahead.

    Frames must be consumed in-order.
    """

    def __init__(self, repo_id: str, tolerance: float, compression: int | None = 95):
        self.repo_id = repo_id
        self.streams: dict[Path, PeekableIterator] = {}
        self.tolerance = tolerance
        self.compression = compression

    def start_downloading(self, path):
        if path not in self.streams:
            self.streams[path] = PeekableIterator(
                stream_rerun_images_from_video_mp(
                    self.repo_id, path, compression=self.compression
                )
            )

    def next_frame(self, path, timestamp):
        self.start_downloading(path)

        (next_frame_ts, next_frame) = self.streams[path].peek()

        while (
            next_frame_ts < timestamp
            and math.fabs(next_frame_ts - timestamp) > self.tolerance
        ):
            next(self.streams[path])
            (next_frame_ts, next_frame) = self.streams[path].peek()
            if next_frame_ts is None:
                return None

        if math.fabs(next_frame_ts - timestamp) < self.tolerance:
            next(self.streams[path])
            return next_frame
        else:
            return None


def stream_rerun_images_from_video(
    repo_id,
    video_path: str,
    frame_queue: multiprocessing.Queue,
    compression: int | None,
):
    """Streams frames from a video file

    Args:
        video_path (Path): Path to the video file
        frame_queue (multiprocessing.Queue): Queue to store the frames
        compression (int | None): Compression level for the images
    """
    cached_video_path = hf_hub_download(repo_id, video_path, repo_type="dataset")

    container = av.open(cached_video_path)

    for frame in container.decode(video=0):
        pts = float(frame.pts * frame.time_base)
        rgb = frame.to_ndarray(format="rgb24")
        img = rr.Image(rgb)
        if compression is not None:
            img = img.compress(jpeg_quality=95)

        frame_queue.put((pts, img))

    frame_queue.put(None)


def stream_rerun_images_from_video_mp(
    repo_id: str, video_path: str, compression: int | None
) -> Any:
    frame_queue: multiprocessing.Queue[(int, rr.Image)] = multiprocessing.Queue(
        maxsize=5
    )

    extractor_proc = multiprocessing.Process(
        target=stream_rerun_images_from_video,
        args=(repo_id, video_path, frame_queue, compression),
    )
    extractor_proc.start()

    while True:
        frame_data = frame_queue.get()
        if frame_data is None:
            break
        yield frame_data

    extractor_proc.join()


def load_from_videos(
    item: dict[str, torch.Tensor],
    video_frame_keys: list[str],
    videos_dir: Path,
    tolerance_s: float,
):
    """Note: When using data workers (e.g. DataLoader with num_workers>0), do not call this function
    in the main process (e.g. by using a second Dataloader with num_workers=0). It will result in a Segmentation Fault.
    This probably happens because a memory reference to the video loader is created in the main process and a
    subprocess fails to access it.
    """
    # since video path already contains "videos" (e.g. videos_dir="data/videos", path="videos/episode_0.mp4")
    data_dir = videos_dir.parent

    for key in video_frame_keys:
        if isinstance(item[key], list):
            # load multiple frames at once (expected when delta_timestamps is not None)
            timestamps = [frame["timestamp"] for frame in item[key]]
            paths = [frame["path"] for frame in item[key]]
            if len(set(paths)) > 1:
                raise NotImplementedError(
                    "All video paths are expected to be the same for now."
                )
            video_path = data_dir / paths[0]

            frames = decode_video_frames_torchvision(
                video_path, timestamps, tolerance_s
            )
            item[key] = frames
        else:
            # load one frame
            timestamps = [item[key]["timestamp"]]
            video_path = data_dir / item[key]["path"]

            frames = decode_video_frames_torchvision(
                video_path, timestamps, tolerance_s
            )
            item[key] = frames[0]

    return item


def decode_video_frames_torchvision(
    video_path: str,
    timestamps: list[float],
    tolerance_s: float,
    device: str = "cpu",
    log_loaded_timestamps: bool = False,
):
    """Loads frames associated to the requested timestamps of a video

    Note: Video benefits from inter-frame compression. Instead of storing every frame individually,
    the encoder stores a reference frame (or a key frame) and subsequent frames as differences relative to
    that key frame. As a consequence, to access a requested frame, we need to load the preceding key frame,
    and all subsequent frames until reaching the requested frame. The number of key frames in a video
    can be adjusted during encoding to take into account decoding time and video size in bytes.
    """
    video_path = str(video_path)

    # set backend
    keyframes_only = False
    if device == "cpu":
        # explicitely use pyav
        torchvision.set_video_backend("pyav")
        keyframes_only = True  # pyav doesnt support accuracte seek
    elif device == "cuda":
        # TODO(rcadene, aliberts): implement video decoding with GPU
        # torchvision.set_video_backend("cuda")
        # torchvision.set_video_backend("video_reader")
        # requires installing torchvision from source, see: https://github.com/pytorch/vision/blob/main/torchvision/csrc/io/decoder/gpu/README.rst
        # check possible bug: https://github.com/pytorch/vision/issues/7745
        raise NotImplementedError(
            "Video decoding on gpu with cuda is currently not supported. Use `device='cpu'`."
        )
    else:
        raise ValueError(device)

    # set a video stream reader
    # TODO(rcadene): also load audio stream at the same time
    reader = torchvision.io.VideoReader(video_path, "video")

    # set the first and last requested timestamps
    # Note: previous timestamps are usually loaded, since we need to access the previous key frame
    first_ts = timestamps[0]
    last_ts = timestamps[-1]

    # access closest key frame of the first requested frame
    # Note: closest key frame timestamp is usally smaller than `first_ts` (e.g. key frame can be the first frame of the video)
    # for details on what `seek` is doing see: https://pyav.basswood-io.com/docs/stable/api/container.html?highlight=inputcontainer#av.container.InputContainer.seek
    reader.seek(first_ts, keyframes_only=keyframes_only)

    # load all frames until last requested frame
    loaded_frames = []
    loaded_ts = []
    for frame in reader:
        current_ts = frame["pts"]
        if log_loaded_timestamps:
            logging.info(f"frame loaded at timestamp={current_ts:.4f}")
        loaded_frames.append(frame["data"])
        loaded_ts.append(current_ts)
        if current_ts >= last_ts:
            break

    reader.container.close()
    reader = None

    query_ts = torch.tensor(timestamps)
    loaded_ts = torch.tensor(loaded_ts)

    # compute distances between each query timestamp and timestamps of all loaded frames
    dist = torch.cdist(query_ts[:, None], loaded_ts[:, None], p=1)
    min_, argmin_ = dist.min(1)

    is_within_tol = min_ < tolerance_s
    assert is_within_tol.all(), (
        f"One or several query timestamps unexpectedly violate the tolerance ({min_[~is_within_tol]} > {tolerance_s=})."
        "It means that the closest frame that can be loaded from the video is too far away in time."
        "This might be due to synchronization issues with timestamps during data collection."
        "To be safe, we advise to ignore this item during training."
    )

    # get closest frames to the query timestamps
    closest_frames = torch.stack([loaded_frames[idx] for idx in argmin_])
    closest_ts = loaded_ts[argmin_]

    if log_loaded_timestamps:
        logging.info(f"{closest_ts=}")

    # convert to the pytorch format which is float32 in [0,1] range (and channel first)
    closest_frames = closest_frames.type(torch.float32) / 255

    assert len(timestamps) == len(closest_frames)
    return closest_frames


def encode_video_frames(imgs_dir: Path, video_path: Path, fps: int):
    """More info on ffmpeg arguments tuning on `lerobot/common/datasets/_video_benchmark/README.md`"""
    video_path = Path(video_path)
    video_path.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg_cmd = (
        f"ffmpeg -r {fps} "
        "-f image2 "
        "-loglevel error "
        f"-i {str(imgs_dir / 'frame_%06d.png')} "
        "-vcodec libx264 "
        "-g 2 "
        "-pix_fmt yuv444p "
        f"{str(video_path)}"
    )
    subprocess.run(ffmpeg_cmd.split(" "), check=True)


@dataclass
class VideoFrame:
    # TODO(rcadene, lhoestq): move to Hugging Face `datasets` repo
    """
    Provides a type for a dataset containing video frames.

    Example:

    ```python
    data_dict = [{"image": {"path": "videos/episode_0.mp4", "timestamp": 0.3}}]
    features = {"image": VideoFrame()}
    Dataset.from_dict(data_dict, features=Features(features))
    ```
    """

    pa_type: ClassVar[Any] = pa.struct({"path": pa.string(), "timestamp": pa.float32()})
    _type: str = field(default="VideoFrame", init=False, repr=False)

    def __call__(self):
        return self.pa_type


with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        "'register_feature' is experimental and might be subject to breaking changes in the future.",
        category=UserWarning,
    )
    # to make VideoFrame available in HuggingFace `datasets`
    register_feature(VideoFrame, "VideoFrame")

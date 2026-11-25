# Dataset format and training pairs

The app records your own front/wrist camera regions, measured robot joints,
commanded follower joints, task instruction, and a human success/failure label.
Files remain local. This is the app's JSON/JPEG format, not an automatically
converted LeRobot dataset or a trained policy.

## Episode files

Each recording has its own `episode_<local-time>/` folder inside
`dataset_collection/episodes/<task>/`. Cleaned copies live under
`dataset_collection/cleaned_episodes/`.

| File | Meaning |
| --- | --- |
| `episode.json` | Outcome, task, timestamps, completion information, joint calibration, camera identity, ROI, and source-code hashes |
| `samples.jsonl` | One JSON object per saved front/wrist image pair, with that pair's attached robot snapshot |
| `robot.jsonl` | Robot control log; after cleaning, retained frame-attached control rows only |
| `front/*.jpg`, `wrist/*.jpg` | ROI images referenced by relative path in each sample; full raw camera images are not recorded |
| `robot_terminal.json` | Final measured observation when available; separate from the frame/action training pairs |

Read image paths from `samples.jsonl`; do not assume that image filenames remain
consecutive after cleaning. Treat the episode folder as the path root. Absolute
paths elsewhere in audit metadata describe the recording computer and are not
needed to load its images on another machine.

## A behavior-cloning pair

For sample `s`, use:

```python
images = (s['cameras']['front']['file'], s['cameras']['wrist']['file'])
state = [s['robot']['follower'][str(i)] for i in range(1, 7)]
action = [s['robot']['command'][str(i)] for i in range(1, 7)]
```

The action is the applied absolute follower target associated with this snapshot.
It is **not** the next measured pose, a Cartesian delta, or the leader's raw joint
position. Channel order is shoulder pan, shoulder lift, elbow flex, wrist flex,
wrist roll, gripper. All six channels are calibrated encoder ticks (4096 ticks
per revolution); the gripper remains continuous, not an open/close class or
finger width in metres. The recorded calibration describes the mapping used for
that robot. Preserve these raw labels when building your training representation.

Do not zip `robot.jsonl` and image rows by line number. Several image pairs may
share the same robot snapshot, and cleaning may deduplicate those control-log
rows. The exact `samples.jsonl` attachment is authoritative.

Use the standard-library reader:

```sh
python examples/read_dataset.py dataset_collection/cleaned_episodes/episode_YOUR_EPISODE
```

Or import it from Python:

```python
from examples.read_dataset import load_episode

metadata, segments = load_episode(episode_folder)
for segment in segments:
    # Build each training window entirely inside this segment.
    for row in segment:
        front_path, wrist_path = row['front'], row['wrist']
        observation = row['state_ticks']
        target = row['action_ticks']
```

The reader checks six finite state/target values, row count/order, and image paths
contained within the episode. It preserves timestamps and returns separate
contiguous segments; it does not normalize, interpolate, drop rows, fit a model,
or issue motor commands. Exclude unpaired frames through the cleaning UI instead
of silently assigning nearby robot commands. Inspect technical errors and the
actual images before selecting episodes for training.

## Editing and boundaries

- **Exclude this frame / Remove no-ops:** remove the image pair and its attached
  robot sample from the cleaned selection. Remaining pairs form one sequence.
  No-op detection compares each pose with the last retained pose. Its tick
  threshold applies to the five arm joints, using both measured and commanded
  positions; any measured or commanded gripper change is retained. It does not
  compare across explicit segment boundaries or missing robot feedback.
  Review proposed exclusions before saving, especially grasps and releases.
- **Remove section & split:** cut a whole interval and retain separate sequences.
  The original-recording editor saves separate episode folders for the parts.
  Editing an existing cleaned copy preserves splits using `segment_id`.
- **Save cleaned copy:** leaves the original recording untouched.
- **Save changes in place:** replaces the selected cleaned episode; removed data
  is not restored by the training reader.

Never form an action chunk across different episode folders or segment IDs.
Handle a short final window explicitly in your own trainer (drop it or use a
padding mask); never fill it using the next episode. Use exactly the retained
frame/command selection rather than restoring removed control-log intervals.

The original timestamps are kept, while `elapsed_seconds` is an edited playback
timeline in cleaned data. `source_index` and `source_elapsed_seconds` identify the
original frame and time. Exclusion joins data; it does not physically smooth a
trajectory or make a large jump safe. Use a split for an unwanted middle section
that represents a separate motion sequence.

## Timing, labels, and training limitations

Leader following requests a control rate of 30 Hz. Camera pairs are saved when
both streams have fresh frames, so the image-pair rate is variable and may be
lower. The cameras are not hardware synchronized. Each pair records camera
capture timestamps, skew, and available robot observation/command timestamps.
Use those values rather than assuming every row is 1/30 second apart. Your
model's training cadence and later physical playback rate are separate choices.

Labels are human judgments. A split part can retain the original episode's
success label with `label_scope: "original_episode"`; that does not establish
that the part independently completes the task. Cleaned episodes may have
`data_complete: false` because they no longer represent the complete physical
recording. Validate their retained pairs for BC instead of rejecting all such
episodes solely on this field.

These files support training your own policy with a trainer that consumes this
format. Choose observation processing, normalization, action representation,
train/validation episode splits, and chunk horizon explicitly. Fit normalization
on training data only, preserve continuous gripper targets unless intentionally
converting them, and retain the robot calibration with exported data.

Clipped observations do not establish lossless offline-RL transitions: removed
actions may have influenced later observations. Any transition/reward conversion
requires an explicit assumption and separate validation. No BC/RL training
service, GPU server, pretrained checkpoint, or policy deployment is included.

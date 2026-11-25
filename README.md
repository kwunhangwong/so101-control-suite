# SO-101 Control Suite

Local macOS apps for configuring two cameras, calibrating an SO-101 leader/follower pair, and collecting and editing visual robot demonstrations. Researchers use their own hardware and keep their data locally.

This release includes **Camera Setup**, **Calibration Check**, and **Dataset Collection**. Evaluation, GPT Bootstrap and DiT + GPT modes are disabled: no remote GPU service, policy backend, Codex account or credentials are distributed. Training is performed in your own environment using the documented data format and example reader. This release is not a policy trainer or a LeRobot dataset exporter.

## Requirements

- macOS 14 or newer with Python 3.10–3.13 and Xcode Command Line Tools (`xcode-select --install`). Python 3.10 is the release verification environment.
- Assembled SO-101 leader and follower, each with six STS3215/model-777 servos, IDs 1–6, in position mode, with appropriate motor power and separate USB serial connections. This app does not assign factory motor IDs.
- Two macOS-compatible cameras. RGB capture and ROI selection work independently of UVC exposure, saturation and contrast controls, which depend on camera support.
- A browser and working USB data cables. No GPU or paid model account is required.

## First setup

1. Download/clone this source, open Terminal in its folder, and run `bash Setup.command`. It creates an isolated `.venv` and installs public dependencies. If macOS blocks downloaded command files, run them using `bash` in Terminal after reviewing the source.
2. Close all app server terminals, connect your devices, then run `bash "Configure Devices.command"`. Choose the front/wrist cameras and identify the leader/follower serial ports. The tool enumerates devices but does not move motors or capture images. It writes your ignored `config.local.json`.
3. Open **Start Camera Setup.command**. Allow the camera permission prompt. Select each camera, drag/resize its ROI, and save. Full-frame defaults are provided. Hardware controls can report unsupported on some cameras while capture continues.
4. Close Camera Setup before using the other apps, so separate capture services do not compete for the cameras. Open **Start Calibration Check.command**. On first run, a “not calibrated” message is expected; placeholder ranges never authorize following or reset.
5. Follow [the calibration guide](docs/CALIBRATION.md), then teach a resting pose. Test a small leader movement with the follower clear of objects before collecting.
6. Open **Start Dataset Collection.command**, check all four devices, enter a task and success condition, and put both arms in similar bent poses. **Start Recording Episode** starts following and saves synchronized-by-host camera/robot samples. **End Recording Episode** stops following and retains holding torque; select Success or Failure to finish.
7. In **Seed data cleaning**, choose a recording, mark unwanted frames or a split section, generate a preview, then save. Original recordings are preserved on export. Loading a cleaned folder enables explicitly confirmed in-place editing.

Calibration and Dataset Collection share one hardware server at `http://127.0.0.1:8766/` and `/dataset/`. Camera Setup uses port 8765. Reopening a launcher reuses its running server. Stop the app server with Control+C before changing configuration. Close all pages to stop camera capture; closing an app does **not** release follower holding torque.

## Dataset use

Recordings are stored under `dataset_collection/episodes/<task>/episode_<local-time>/`. Cleaned copies are under `dataset_collection/cleaned_episodes/`. Camera images are ROI-only JPEGs. Each retained `samples.jsonl` row contains the paired image paths, measured follower positions, and commanded targets for that same sample.

See [data format and training boundaries](docs/DATA_FORMAT.md) and `examples/read_dataset.py`. Motor positions and targets are six absolute encoder-tick values, not Cartesian deltas. A training pipeline must respect explicit segment boundaries and account for variable image cadence. Dataset labels do not guarantee successful physical execution or RL suitability.

## Operating the robot

- Calibration Test and seed following have no application speed cap. A large initial leader/follower pose gap can cause a sudden movement. Start with matching poses and small movements.
- Clear Error & Reset uses a slow ramp capped at 10 degrees/second. Clear the path and remove gripper obstructions first. No saved pose is guaranteed collision-free in every scene.
- Stop & hold retains torque. Support the follower before using the explicit Release torque dialog or switching off power.
- Gripper Force adjusts motor 6's output limit, not measured force in newtons. A lower setting can still overload when a commanded closure pushes against an object. Hardware faults and serial failures stop commands; software tracking warnings do not.
- Servers bind only to loopback. They are local device-control tools, not authenticated multi-user web services; do not expose them to your LAN or the internet.

## Private configuration and optional policies

Your device IDs, calibration, resting pose, camera settings, recordings, logs and generated helpers are ignored by Git. Check the exact staged file list before sharing changes; an ignore file does not sanitize files already committed.

Policy inference is deliberately absent from this release. Users who implement an adapter must supply and secure their own model endpoint/SSH configuration and model credentials outside source control. Configuring credentials alone will not enable the disabled modes. Keep such integration separate from calibration and the recorder's frame/action contract.

## Development checks

Run `.venv/bin/python -m unittest discover -s tests -v`. Tests use temporary synthetic episodes and fake motors; they do not open serial ports or cameras. Native camera helpers are compiled locally at first use. Software verification does not substitute for checking a new robot's assembly, calibration and camera mounting.

Apache-2.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE) for third-party attribution and excluded policy code.

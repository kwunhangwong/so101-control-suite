# Calibrating your own SO-101 pair

1. Verify USB roles using Configure Devices. Each arm needs its own controller port and six uniquely addressed motors. Do not swap roles by guessing serial-port order.
2. Support both arms securely. In Calibration Check, click **Re-calibration**, confirm that both arms are supported, and choose **Start new calibration**. This explicitly turns off torque so joints can be moved by hand.
3. Put both arms in corresponding center poses, including a half-open gripper. Use the [LeRobot SO-101 calibration pose guide](https://huggingface.co/docs/lerobot/so101#calibrate) to identify the assembly's center pose. Click **Capture centers** while holding still.
4. Move each joint on each arm through its comfortable mechanical range, without forcing a stop. The app samples the minimum/maximum. It requires range coverage on both sides of the captured center. Wrist roll retains the full single-turn encoder range.
5. Review the range table, confirm coverage, then **Save calibration**. Save writes offsets/ranges to both arms and verifies readback. It creates a local backup and checkpoint. Torque remains off after saving. Cancel before saving leaves the calibration unchanged.
6. With the follower supported, place the leader in a bent central resting pose with a suitable gripper opening. Click **Teach resting pose**. This reads the leader and saves its calibrated follower equivalent; it does not move the follower.
7. Clear the follower's path, then use **Clear Error & Reset** to move slowly to the saved pose. Use **Stop & hold** if needed. Put the leader near that pose before starting **Calibration Test**; test one small joint movement at a time.

Changing arm port identities invalidates the configured calibration until calibration is performed again. Recalibration invalidates a resting pose whose calibration no longer matches. Saved checkpoints include their USB identities and may restore a matching resting pose. Do not reuse another robot's checkpoint.

If saving fails, the app tries to restore prior motor values. An incomplete rollback leaves a persistent recovery marker and blocks normal connection. Keep the backup, resolve the hardware/communication failure, and use the calibration workflow to recover; do not delete the marker to bypass the check.

To release holding torque, open `http://127.0.0.1:8766/?release=1`, support the follower, and explicitly confirm **Release torque**. Closing the page/server retains torque. Releasing torque or removing motor power can make the arm fall.

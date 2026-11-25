"""Shared selected camera pairs for BC and explicitly approximate edited RL."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from cleaning import image_path, paired_control_rows

SCHEMA='so101_approximate_selected_frame_replay_v3'
OLD_SCHEMA='so101_unedited_rl_record_v1'

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def rows(path):return [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]
def source_id(sample):return sample.get('source_index',sample['index'])
def write_json(path,value):
 temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,indent=2,allow_nan=False)+'\n');temporary.replace(path)

def finalize_episode(folder,metadata):
 """Rebuild from selected pairs only. The final pair supplies the RL ending state."""
 folder=Path(folder).resolve()
 if metadata.get('status')!='saved' or not metadata.get('robot_finished') or metadata.get('label') not in ('success','failure'):
  raise ValueError('Finish and label the episode before preparing shared data.')
 samples=rows(folder/'samples.jsonl');log=rows(folder/'robot.jsonl')
 if len(samples)!=metadata['frames']:raise ValueError('Saved frame count changed.')
 paired=paired_control_rows(samples,log)
 replay=folder/'rl_replay';motor_count=metadata.get('original_motor_command_count',metadata.get('control_steps',0))
 if replay.exists():
  old=json.loads((replay/'manifest.json').read_text())
  if old.get('schema')==OLD_SCHEMA:
   for name,entry in old['files'].items():
    if sha(replay/name)!=entry['sha256']:raise ValueError(f'Original archive changed: {name}')
   original=json.loads((replay/'metadata.json').read_text());motor_count=original.get('control_steps',motor_count)
  elif old.get('schema')!=SCHEMA:raise ValueError('Unknown replay format; no files replaced.')
 refs={s['cameras'][c]['file'] for s in samples for c in ('front','wrist')}
 for name in refs:image_path(folder,name).stat()
 segments=[]
 for i,sample in enumerate(samples):
  if not segments or sample.get('segment_id',0)!=samples[i-1].get('segment_id',0):segments.append([i,i+1])
  else:segments[-1][1]=i+1
 windows=[]
 for first,end in segments:
  # N selected pairs provide N-1 approximate transitions; the last is the ending observation.
  for start in range(first,end-8):
   next_index=start+8;terminal=next_index==len(samples)-1
   windows.append({'start_pair':start,'action_pairs':list(range(start,next_index)),
                   'next_pair':next_index,'terminal':terminal,
                   'reward':float(terminal and metadata['label']=='success'),
                   'successor_start':next_index if next_index+8<end else None})
 if metadata.get('error'):windows=[]
 ticks=[s['robot']['command']['6'] for s in samples]
 threshold=(min(ticks)+max(ticks))/2 if ticks and min(ticks)<max(ticks) else None
 threshold_source='kept_command_extrema_midpoint'
 if ticks and threshold is None:
  gripper=metadata['context'].get('execution_gripper',{})
  if gripper.get('closed_endpoint_tick') is not None and gripper.get('open_endpoint_tick') is not None:
   threshold=(gripper['closed_endpoint_tick']+gripper['open_endpoint_tick'])/2
   threshold_source='recorded_execution_endpoints_for_constant_gripper_clip'
 terminal=None
 if samples:
  last=samples[-1];robot=last['robot']
  terminal={'follower':robot['follower'],'observed_at':robot['observation_received_at'],
            'observed_monotonic_ns':robot.get('observation_monotonic_ns'),
            'cameras':last['cameras'],'source_pair':len(samples)-1,'source_index':source_id(last),
            'kind':'selected_final_frame','physical_terminal':False,
            'note':'Ending observation for approximate edited replay; not a measured result of the final row command.'}
 stage=Path(tempfile.mkdtemp(prefix='.rl-index-',dir=folder));backup=folder/'.rl-index-previous'
 if backup.exists():shutil.rmtree(stage);raise ValueError('Unfinished replay replacement needs recovery.')
 original_files={name:(folder/name).read_bytes() if (folder/name).exists() else None for name in ('robot.jsonl','robot_terminal.json','episode.json')}
 original_metadata=metadata.copy();published=False;committed=False
 try:
  (stage/'windows.jsonl').write_text(''.join(json.dumps(w)+'\n' for w in windows))
  manifest={'schema':SCHEMA,'approximate':True,'selection_authority':'../samples.jsonl',
            'action_source':'selected samples.jsonl robot.command; original removed commands never restored',
            'state_source':'selected samples.jsonl robot.follower',
            'transition_assumption':'Consecutive retained pairs are treated as adjacent edited replay states; effects of removed actions may remain in recorded observations.',
            'reward_source':'human retained-episode label at selected final observation',
            'final_pair_role':'BC pair and RL terminal observation; its command has no following selected state and is not an RL action',
            'original_timestamps_preserved':True,'cadence':'one retained camera frame/action pair per edited step',
            'segments':segments,'bc_pairs':len(samples),'rl_windows':len(windows),'horizon':16,'executed_actions':8,
            'samples_sha256':sha(folder/'samples.jsonl'),'windows_sha256':sha(stage/'windows.jsonl'),
            'image_sha256':{name:sha(folder/name) for name in sorted(refs)},
            'gripper_threshold_tick':threshold,'gripper_threshold_source':threshold_source,
            'normalization':'Use frozen SO101 checkpoint statistics once; no normalization fit on removed data.'}
  write_json(stage/'manifest.json',manifest)
  if replay.exists():replay.rename(backup)
  try:stage.rename(replay);published=True
  except Exception:
   if backup.exists():backup.rename(replay)
   raise
  temporary=folder/'robot.jsonl.compact.tmp';temporary.write_text(''.join(json.dumps(r)+'\n' for r in paired));temporary.replace(folder/'robot.jsonl')
  if terminal:write_json(folder/'robot_terminal.json',terminal)
  elif (folder/'robot_terminal.json').exists():(folder/'robot_terminal.json').unlink()
  metadata.setdefault('original_motor_command_count',motor_count)
  metadata.update(control_steps=len(paired),recorded_robot_samples=len(paired),terminal_observation_valid=bool(terminal),
                  terminal_observation_source='selected_final_frame',frame_action_pairing='exact_retained_samples_robot_v1')
  metadata['rl_replay']={'path':'rl_replay','schema':SCHEMA,'manifest_sha256':sha(replay/'manifest.json'),'approximate':True}
  metadata['training_data']={'schema':SCHEMA,'approximate_rl':True,'bc_pairs':len(samples),'rl_windows':len(windows),
                             'selection':'saved camera frame/action pairs only','recording_cadence':'unchanged camera-frame cadence',
                             'reference_actual_execution_replay':False}
  write_json(folder/'episode.json',metadata)
  committed=True
  if backup.exists():shutil.rmtree(backup)
  for name in ('front','wrist','policy_inputs'):
   path=folder/name
   if path.exists():
    for image in path.glob('*.jpg'):
     if image.relative_to(folder).as_posix() not in refs:image.unlink()
    if not any(path.iterdir()):path.rmdir()
 except Exception:
  if published and not committed:
   shutil.rmtree(replay)
   if backup.exists():backup.rename(replay)
   for name,content in original_files.items():
    path=folder/name
    if content is None:path.unlink(missing_ok=True)
    else:
     temporary=folder/(name+'.restore.tmp');temporary.write_bytes(content);temporary.replace(path)
   metadata.clear();metadata.update(original_metadata)
  raise
 finally:
  if stage.exists():shutil.rmtree(stage)
 return metadata['rl_replay']

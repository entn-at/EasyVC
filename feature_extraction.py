import argparse
import yaml
import json
import os
import glob
from preprocess.audio_utils import mel_spectrogram, normalize
from prosodic_encoder.ppgvc_f0.ppgvc_lf0 import compute_f0 as compute_ppgvc_f0
from prosodic_encoder.fastspeech2_pitch_energy.pitch_energy import extract_pitch_energy as compute_fastspeech2_pitch_energy
from decoder.vits.mel_processing import spectrogram_torch 
import pyworld as pw
import librosa
import numpy  as np
from tqdm import tqdm
import csv
from concurrent.futures import ProcessPoolExecutor
from functools import partial
import subprocess
import torch




def bigvgan_logmelspectrogram(audio, config):
    audio = normalize(audio) * 0.95
    audio = torch.FloatTensor(audio).unsqueeze(0)
    mel = mel_spectrogram(audio, config['n_fft'], config['num_mels'],
          config['sampling_rate'], config['hop_size'], config['win_size'], config['fmin'], config['fmax'],
          center=False)
    mel = mel.squeeze(0).T.numpy()
    return mel
    

def ppgvc_hifigan_logmelspectrogram(audio, config):
    
    # extract mel   
    norm_audio = normalize(audio) * 0.95
    norm_audio = torch.FloatTensor(norm_audio).unsqueeze(0)
    mel = mel_spectrogram(
           norm_audio,
           sampling_rate = config['sampling_rate'],
           n_fft = config['fft_size'],
           hop_size = config['hop_size'],
           win_size = config['win_length'],
           num_mels = config['num_mels'],
           fmin = config['fmin'],
           fmax = config['fmax'],
    )
    mel = mel.squeeze(0).T.numpy()
    # min-max normalization
    mel = (mel - config['mel_min']) / (config['mel_max'] - config['mel_min']) * 8.0 - 4.0 
    mel = np.clip(mel, -4. , 4.)
    return mel
    
       
def logmelfilterbank(
    audio,
    sampling_rate,
    fft_size=1024,
    hop_size=256,
    win_length=None,
    window="hann",
    num_mels=80,
    fmin=None,
    fmax=None,
    eps=1e-10,
    log_base=10.0,
):
    """Compute log-Mel filterbank feature.

    Args:
        audio (ndarray): Audio signal (T,).
        sampling_rate (int): Sampling rate.
        fft_size (int): FFT size.
        hop_size (int): Hop size.
        win_length (int): Window length. If set to None, it will be the same as fft_size.
        window (str): Window function type.
        num_mels (int): Number of mel basis.
        fmin (int): Minimum frequency in mel basis calculation.
        fmax (int): Maximum frequency in mel basis calculation.
        eps (float): Epsilon value to avoid inf in log calculation.
        log_base (float): Log base. If set to None, use np.log.

    Returns:
        ndarray: Log Mel filterbank feature (#frames, num_mels).

    """
    # get amplitude spectrogram
    x_stft = librosa.stft(
        audio,
        n_fft=fft_size,
        hop_length=hop_size,
        win_length=win_length,
        window=window,
        pad_mode="reflect",
    )
    spc = np.abs(x_stft).T  # (#frames, #bins)

    # get mel basis
    fmin = 0 if fmin is None else fmin
    fmax = sampling_rate / 2 if fmax is None else fmax
    mel_basis = librosa.filters.mel(sampling_rate, fft_size, num_mels, fmin, fmax)
    mel = np.maximum(eps, np.dot(spc, mel_basis.T))

    if log_base is None:
        return np.log(mel)
    elif log_base == 10.0:
        return np.log10(mel)
    elif log_base == 2.0:
        return np.log2(mel)
    else:
        raise ValueError(f"{log_base} is not supported.")


def process_speaker(spk_meta, config, args):
    
    for row in tqdm(spk_meta):
        # load wav
        ID = row['ID']
        wav_path = row['wav_path'].strip()
        spk = row['dataset'] + '_'+ row['spk'] if 'dataset' in row else row['spk']
        audio, fs = librosa.load(wav_path, sr = config['sampling_rate'])
        # trim silence
        if 'start' in row and 'end' in row:
            start, end = float(row['start']), float(row['end'])
            audio = audio[ int(start * config['sampling_rate']):
                            int(end * config['sampling_rate'])
                ]
        
        if args.feature_type == 'mel':
            feature = logmelfilterbank(
                audio,
                sampling_rate=config['sampling_rate'],
                hop_size=config['hop_size'],
                fft_size=config["fft_size"],
                win_length=config["win_length"],
                window=config["window"],
                num_mels=config["num_mels"],
                fmin=config["fmin"],
                fmax=config["fmax"]
            )
        elif args.feature_type == 'ppgvc_mel':
            feature = ppgvc_hifigan_logmelspectrogram(audio, config)     
        elif args.feature_type == 'bigvgan_mel':
            feature = bigvgan_logmelspectrogram(audio, config)    
        elif args.feature_type == 'ppgvc_f0':
            feature = compute_ppgvc_f0(audio, sr = config['sampling_rate'], frame_period = 10.0)    
        elif args.feature_type == 'fastspeech2_pitch_energy':
            feature = compute_fastspeech2_pitch_energy(audio, config)     
        elif args.feature_type == "vits_spec":
            audio = torch.FloatTensor([audio.astype(np.float32)])
            feature = spectrogram_torch(audio, config['n_fft'], 
                config['sampling_rate'],
                config['hop_size'],
                config['win_size']
                )
            feature = feature[0].data.numpy().T     
        feature_path = os.path.join(args.dump_dir, args.split, args.feature_type, spk, ID+'.npy')
        os.makedirs(os.path.dirname(feature_path), exist_ok = True)
        np.save(feature_path, feature)
    return 0    
    
    
if __name__ == '__main__':
    
    parser = argparse.ArgumentParser()

    parser.add_argument('--metadata', type = str)
    parser.add_argument('--config_path', type = str)
    parser.add_argument('--dump_dir', type = str)
    parser.add_argument('--split', type = str)
    parser.add_argument('--max_workers', type = int, default = 20)
    parser.add_argument('--speaker', type = str, default = None)
    parser.add_argument('--feature_type', type = str, default = 'mel', 
        choices = ['mel','ppgvc_mel', 'bigvgan_mel', 'ppgvc_f0', 'fastspeech2_pitch_energy', 'vits_spec'])
    parser.add_argument('--pitch', default = False, action = 'store_true')
    parser.add_argument('--sge_task_id', type = int, default = None)
    parser.add_argument('--sge_n_tasks', type = int, default = None)
    args = parser.parse_args()
    
    # load in config
    with open(args.config_path) as f:
        config = yaml.safe_load(f)
        f.close()
    print(config)   
    
    # build a dict for spk2metadata
    all_metadata = []
    spk2meta = {}
    with open(args.metadata) as f:
        reader = csv.DictReader(f)
        for row in reader:
            _spk = row['spk']
            if _spk not in spk2meta:
                spk2meta[_spk] = []
            all_metadata.append(row)
            
            spk2meta[_spk].append(row)         
        
        f.close()
    
    if args.speaker is not None:
        # only for one speaker
        if args.speaker not in spk2meta:
            raise Exception(f"speaker {speaker} should be in the metadata")

        spk_meta = spk2meta[args.speaker]
        process_speaker(spk_meta, config, args)
    elif args.sge_task_id is not None:
        
        n_per_task = np.ceil(len(all_metadata) / args.sge_n_tasks)    
        start = int(( args.sge_task_id -1 ) * n_per_task)
        if int( args.sge_task_id * n_per_task) >= len(all_metadata):
            end = len(all_metadata) 
        else:
            end = int(args.sge_task_id  * n_per_task)    
        print(f'selected_metadata from {start} to {end}', flush = True)    
        selected_metadata = all_metadata[start: end]
        process_speaker(selected_metadata, config, args)
    
    else:
        # process all speakers
        
        # set up processes    
        executor = ProcessPoolExecutor(max_workers=args.max_workers)
        futures = []
        for spk in spk2meta:
            spk_meta = spk2meta[spk]        
        
            futures.append(executor.submit(partial(process_speaker, spk_meta, config, args)))
        results = [future.result() for future in tqdm(futures)]    

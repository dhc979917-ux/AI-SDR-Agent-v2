"""Local bounded decoding and contiguous, silence-aware ASR segments."""
from array import array
from contextlib import contextmanager
from pathlib import Path
import io
import subprocess
import sys
import tempfile
import threading
import wave

RATE = 16000
PIPELINE_VERSION = 'pcm16-16k-mono-silence-v1'
MIN_GAP_SECONDS = 0.2
_VAD_LOCK = threading.Lock()
_VAD_CACHE = {}


class AudioError(Exception):
    pass


@contextmanager
def normalized_audio(source, directory, settings):
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        executable = get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        raise AudioError('长录音处理组件未安装，请执行 pip install -r requirements.txt 后重试') from None
    with tempfile.TemporaryDirectory(prefix='.processing-', dir=directory) as work:
        output = Path(work) / 'normalized.wav'
        command = [executable, '-nostdin', '-hide_banner', '-loglevel', 'error', '-xerror',
                   '-protocol_whitelist', 'file,pipe', '-format_whitelist', 'wav,mp3,mov,flac,ogg',
                   '-i', str(Path(source).resolve()), '-map', '0:a:0', '-vn', '-sn', '-dn',
                   '-ac', '1', '-ar', str(RATE), '-c:a', 'pcm_s16le',
                   '-t', str(settings.audio_max_seconds+1), '-threads', '1', '-f', 'wav', str(output)]
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=settings.audio_decode_timeout,
                                    check=False)
        except subprocess.TimeoutExpired:
            raise AudioError('录音解码超时，原文件已保存，可重新导出为标准WAV或MP3后上传') from None
        except OSError:
            raise AudioError('无法启动本机音频处理组件，请检查FFmpeg安装') from None
        if result.returncode or not output.exists():
            raise AudioError('录音解码失败，可能是文件不完整或编码不兼容；请重新导出为标准WAV或MP3')
        output.chmod(0o600)
        try:
            with wave.open(str(output), 'rb') as audio:
                duration = audio.getnframes()/RATE
                if audio.getnchannels()!=1 or audio.getsampwidth()!=2 or audio.getframerate()!=RATE or not audio.getnframes():
                    raise AudioError('录音解码后没有有效音频数据')
                if duration > settings.audio_max_seconds:
                    raise AudioError(f'实际解码时长超过{settings.audio_max_seconds/60:g}分钟，请拆分录音后上传')
        except (wave.Error, EOFError):
            raise AudioError('录音转换结果不完整，请重新导出音频') from None
        yield output


def vad_speech_timestamps(wav_path):
    """Speech spans [(start,end)] in seconds via optional silero-vad; None keeps the energy heuristic."""
    with _VAD_LOCK:
        try:
            from silero_vad import load_silero_vad, read_audio, get_speech_timestamps
        except ImportError:
            return None
        try:
            if 'model' not in _VAD_CACHE:
                _VAD_CACHE['model'] = load_silero_vad()
            wav = read_audio(str(wav_path), sampling_rate=RATE)
            stamps = get_speech_timestamps(wav, _VAD_CACHE['model'], return_seconds=True)
        except Exception:
            return None
    spans = sorted((max(float(s.get('start') or 0), 0.0), float(s.get('end') or 0)) for s in stamps)
    spans = [(start, end) for start, end in spans if end > start]
    return spans or None


def _energy_cut(audio, end, lower, window):
    following_quiet = False
    for offset in range(end-window, lower-1, -window):
        audio.setpos(offset)
        values = array('h', audio.readframes(window))
        if sys.byteorder != 'little':
            values.byteswap()
        quiet = bool(values) and max(values)<300 and min(values)>-300
        if quiet and following_quiet:
            return offset+window
        following_quiet = quiet
    return None


def _speech_cut(lower, end, speech):
    """Cut at the midpoint of the widest silence gap inside [lower,end) when it is wide enough."""
    spans, current = [], lower
    for start, stop in speech:
        start, stop = int(start*RATE), int(stop*RATE)
        if stop <= current:
            continue
        if start >= end:
            break
        if start > current:
            spans.append((current, start))
        current = max(current, min(stop, end))
    if current < end:
        spans.append((current, end))
    best = None
    for start, stop in spans:
        if stop-start >= MIN_GAP_SECONDS*RATE and (best is None or stop-start > best[1]-best[0]):
            best = (start, stop)
    return (best[0]+best[1])//2 if best else None


def segment_ranges(audio, seconds, speech=None):
    """Each input frame belongs to exactly one segment; never remove repeated speech."""
    if not 1 <= seconds <= 180:
        raise AudioError('分段配置无效，每段须在1到180秒之间')
    total, start, window = audio.getnframes(), 0, RATE//5
    result = []
    while start < total:
        end = min(start+seconds*RATE, total)
        if end < total:
            lower = max(start+int(seconds*.75)*RATE, end-8*RATE)
            cut = _speech_cut(lower, end, speech) if speech else _energy_cut(audio, end, lower, window)
            if cut is not None:
                end = cut
        result.append((start,end))
        start = end
    return result


def wav_bytes(frames):
    output = io.BytesIO()
    with wave.open(output,'wb') as writer:
        writer.setnchannels(1);writer.setsampwidth(2);writer.setframerate(RATE)
        writer.writeframes(frames)
    return output.getvalue()

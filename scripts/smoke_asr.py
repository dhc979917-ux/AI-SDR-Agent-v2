"""Explicit cloud ASR test for a caller-selected, authorized recording."""
from pathlib import Path
import argparse
import hashlib
import json
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sdr.audio import inspect_audio, transcribe
from sdr.config import ROOT, Settings


def main():
    parser=argparse.ArgumentParser(description='将指定录音发送到已配置ASR服务；只使用已获授权的数据，调用可能计费')
    parser.add_argument('--audio',type=Path,required=True)
    parser.add_argument('--synthetic',action='store_true',help='将本次报告标记为合成测试数据')
    args=parser.parse_args()
    settings=Settings()
    if not settings.asr_key:
        parser.error('尚未配置SDR_ASR_API_KEY或DASHSCOPE_API_KEY')
    data=args.audio.read_bytes()
    mime,duration=inspect_audio(data,args.audio.name,settings)
    if duration > 300 or len(data) > 10_000_000:
        parser.error('此脚本仅验证单次ASR接口（≤5分钟、≤10MB）；长录音请从工作台上传以验证自动分段与断点重试')
    start=time.monotonic()
    text,provider=transcribe(data,mime,settings)
    result={'filename':args.audio.name,'sha256':hashlib.sha256(data).hexdigest(),'duration':duration,
            'data_mode':'synthetic' if args.synthetic else 'authorized_recording',
            'mime':mime,'text':text,'provider':provider,'elapsed_ms':round((time.monotonic()-start)*1000)}
    output=ROOT/'eval/artifacts'/('asr-format-'+args.audio.suffix.lstrip('.')+'.json')
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({'status':'passed','duration':duration,'elapsed_ms':result['elapsed_ms'],'report':str(output)},ensure_ascii=False))


if __name__=='__main__':
    main()

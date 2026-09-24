from __future__ import annotations
import math
import shutil
import subprocess
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont
from config import settings

WIDTH=1080
HEIGHT=1920

def _font(size:int):
    candidates=[
        Path(settings.font_path),
        Path(r"C:\Windows\Fonts\meiryo.ttc"),
        Path(r"C:\Windows\Fonts\YuGothM.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path),size=size)
    return ImageFont.load_default()

def _wrap(text:str,max_chars:int=17)->list[str]:
    text=text.strip()
    return [text[i:i+max_chars] for i in range(0,len(text),max_chars)] or [""]

def _chunks(script:str,max_chars:int=34)->list[str]:
    script=script.replace("。","。|").replace("！","！|").replace("？","？|")
    pieces=[p.strip() for p in script.split("|") if p.strip()]
    out=[]
    buf=""
    for piece in pieces:
        if len(buf)+len(piece)<=max_chars:
            buf+=piece
        else:
            if buf:
                out.append(buf)
            buf=piece
    if buf:
        out.append(buf)
    return out or [script[:max_chars]]

def _make_frame(title:str,text:str,character_name:str,path:Path,index:int,total:int)->None:
    img=Image.new("RGB",(WIDTH,HEIGHT),(18,22,30))
    draw=ImageDraw.Draw(img)
    title_font=_font(50)
    body_font=_font(76)
    small_font=_font(38)

    draw.rounded_rectangle((70,120,1010,310),radius=28,fill=(31,38,52))
    for i,line in enumerate(_wrap(title,21)[:2]):
        draw.text((105,155+i*65),line,font=title_font,fill=(245,245,245))

    lines=_wrap(text,14)
    block_h=len(lines)*108
    y=max(520,(HEIGHT-block_h)//2)
    for line in lines:
        bbox=draw.textbbox((0,0),line,font=body_font)
        x=(WIDTH-(bbox[2]-bbox[0]))//2
        draw.text((x,y),line,font=body_font,fill=(255,255,255))
        y+=108

    draw.text((80,1710),f"{character_name} / AI YouTuber",font=small_font,fill=(205,210,220))
    draw.text((80,1770),f"{index+1}/{total}",font=small_font,fill=(155,165,180))
    path.parent.mkdir(parents=True,exist_ok=True)
    img.save(path)

def _audio_duration(audio_path:Path)->float:
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe が見つかりません。FFmpegをインストールしてください。")
    cmd=[
        "ffprobe","-v","error","-show_entries","format=duration",
        "-of","default=noprint_wrappers=1:nokey=1",str(audio_path)
    ]
    return float(subprocess.check_output(cmd,text=True).strip())

def render_short(title:str,script:str,audio_path:Path,output_path:Path,character_name:str)->Path:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg が見つかりません。FFmpegをインストールしてください。")

    duration=max(_audio_duration(audio_path),1.0)
    chunks=_chunks(script)
    per=duration/len(chunks)
    work=output_path.parent/f".frames_{output_path.stem}"
    work.mkdir(parents=True,exist_ok=True)

    frames=[]
    for i,chunk in enumerate(chunks):
        frame=work/f"frame_{i:03d}.png"
        _make_frame(title,chunk,character_name,frame,i,len(chunks))
        frames.append(frame)

    concat=work/"concat.txt"
    lines=[]
    for frame in frames:
        lines.append(f"file '{frame.resolve().as_posix()}'")
        lines.append(f"duration {per:.4f}")
    lines.append(f"file '{frames[-1].resolve().as_posix()}'")
    concat.write_text("\n".join(lines),encoding="utf-8")

    output_path.parent.mkdir(parents=True,exist_ok=True)
    cmd=[
        "ffmpeg","-y",
        "-f","concat","-safe","0","-i",str(concat),
        "-i",str(audio_path),
        "-c:v","libx264","-preset","medium","-r","30",
        "-pix_fmt","yuv420p","-c:a","aac","-b:a","192k",
        "-shortest",str(output_path)
    ]
    subprocess.run(cmd,check=True)
    shutil.rmtree(work,ignore_errors=True)
    return output_path

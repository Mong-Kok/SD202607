import os, textwrap, io, json, base64
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from PIL import Image, ImageDraw, ImageFont, ImageOps
from google import genai
from google.genai import types

# --- 設定 ---
API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_API_KEY_HERE")
client = genai.Client(api_key=API_KEY)
app = FastAPI(title="Bangkok Cable Analyzer")

# --- データ構造定義 --- 
class Annotation(BaseModel):
    title: str = Field(description="要素の名称")
    description: str = Field(description="技術的背景を含む詳細な解説")
    target_x: float = Field(description="対象のX座標 (0.0=左, 1.0=右)")
    target_y: float = Field(description="対象のY座標 (0.0=上, 1.0=下)")
    # 不要になった box_x, box_y を削除

class AnnotationList(BaseModel):
    annotations: list[Annotation]

# --- 外部プロンプト読み込み ---
def load_extra_info():
    def read_file(path, default):
        return open(path, "r", encoding="utf-8").read() if os.path.exists(path) else default
    persona = read_file("persona.txt", "あなたはバンコクの通信インフラ専門家です。")
    research = read_file("research_data.md", "バンコクの電線は、色や高さで識別されています。")
    return f"{persona}\n\n参考ナレッジ:\n{research}\n\n※座標系: 左上(0,0), 右下(1,1)"

# --- 画像描画ロジック ---
def draw_annotations(img, annotations):
    # 半透明描画を正しく機能させるためのオーバーレイ画像を作成
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    w, h = img.size
    
    # サイズ設定（1/2スケール）
    base_scale = max(w, h) / 1000.0
    ann_scale = base_scale * 0.5 
    
    font_paths = [
        "/System/Library/Fonts/Hiragino Sans W4.ttc", "/System/Library/Fonts/ヒラギノ角ゴシック W4.ttc",
        "C:\\Windows\\Fonts\\msgothic.ttc", "C:\\Windows\\Fonts\\meiryo.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc"
    ]
    font_path = next((p for p in font_paths if os.path.exists(p)), None)
    
    title_size, desc_size = int(25 * ann_scale), int(20 * ann_scale)
    f_title = ImageFont.truetype(font_path, title_size) if font_path else ImageFont.load_default()
    f_desc = ImageFont.truetype(font_path, desc_size) if font_path else ImageFont.load_default()

    # 吹き出しを上段(Top)と下段(Bottom)に分類
    top_group = []
    bottom_group = []
    
    for ann in annotations:
        wrapped = textwrap.fill(ann.description, width=15)
        line_count = len(wrapped.split('\n'))
        tw = 340 * ann_scale # 計算をシンプル化 (680/2)
        th = title_size + (desc_size * 1.3 * line_count) + (30 * ann_scale)
        
        item = {"ann": ann, "tw": tw, "th": th, "wrapped": wrapped}
        
        if ann.target_y < 0.5:
            top_group.append(item)
        else:
            bottom_group.append(item)

    top_group.sort(key=lambda x: x["ann"].target_x)
    bottom_group.sort(key=lambda x: x["ann"].target_x)

    def layout_and_draw(group, is_top):
        if not group: return
        
        count = len(group)
        margin = 20 * ann_scale
        total_width = sum(item["tw"] for item in group) + (margin * (count - 1))
        
        current_x = (w - total_width) / 2
        
        for item in group:
            ann = item["ann"]
            tx, ty = ann.target_x * w, ann.target_y * h
            bx = current_x
            by = margin if is_top else h - item["th"] - margin
            
            # ボックスの境界座標
            rect_l, rect_t, rect_r, rect_b = bx, by, bx + item["tw"], by + item["th"]
            
            # 引出線の接続点（ターゲットとの位置関係から最適な辺を動的に選択）
            if ty > rect_b:
                lx, ly = bx + item["tw"] / 2, rect_b      # ターゲットが下 -> 下辺の中央
            elif ty < rect_t:
                lx, ly = bx + item["tw"] / 2, rect_t      # ターゲットが上 -> 上辺の中央
            elif tx > rect_r:
                lx, ly = rect_r, by + item["th"] / 2      # ターゲットが右 -> 右辺の中央
            else:
                lx, ly = rect_l, by + item["th"] / 2      # ターゲットが左 -> 左辺の中央
            
            # 引き出し線 (半透明イエロー)
            draw.line([(tx, ty), (lx, ly)], fill=(255, 235, 59, 180), width=max(1, int(4*ann_scale)))
            
            # ターゲット点
            r = 8 * ann_scale
            draw.ellipse([tx-r, ty-r, tx+r, ty+r], fill=(255, 0, 0, 255), outline=(255, 255, 255), width=max(1, int(2*ann_scale)))
            
            # ボックス (半透明ブラック - 不透明度を128から180に調整し視認性を向上)
            draw.rounded_rectangle([bx, by, bx+item["tw"], by+item["th"]], radius=10*ann_scale, fill=(0, 0, 0, 180))
            
            # テキスト (完全不透明)
            draw.text((bx + 15*ann_scale, by + 10*ann_scale), ann.title, font=f_title, fill=(255, 215, 0, 255))
            draw.text((bx + 15*ann_scale, by + 15*ann_scale + title_size), item["wrapped"], font=f_desc, fill=(255, 255, 255, 255))
            
            current_x += item["tw"] + margin

    layout_and_draw(top_group, True)
    layout_and_draw(bottom_group, False)
    
    # 元画像とオーバーレイを透過合成
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

# --- エンドポイント --- 
@app.get("/", response_class=HTMLResponse)
async def index():
    return """<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
    <body style="font-family:sans-serif; text-align:center; padding:20px; background:#f0f0f0;">
    <h2>バンコク電線解析メガネ v3</h2><form action="/analyze" method="post" enctype="multipart/form-data">
    <input type="file" name="file" accept="image/*" capture="environment" style="margin:20px 0;"><br>
    <button type="submit" style="padding:15px 30px; font-size:18px; border-radius:10px; background:#007bff; color:white;">解析開始</button></form></body></html>"""

@app.post("/analyze", response_class=HTMLResponse)
async def analyze(file: UploadFile = File(...)):
    # 無駄な Image.new + paste の処理を省略し、直接 RGBA に変換
    raw_img = Image.open(io.BytesIO(await file.read()))
    clean_img = ImageOps.exif_transpose(raw_img).convert("RGBA")
    
    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[clean_img, "画像を分析し、通信インフラの要素を特定して座標と解説を出力してください。"],
        config=types.GenerateContentConfig(
            system_instruction=load_extra_info(), 
            response_mime_type="application/json", 
            response_schema=AnnotationList, 
            temperature=0.2
        )
    )
    
    annotated_img = draw_annotations(clean_img, AnnotationList(**json.loads(resp.text)).annotations)
    
    buf = io.BytesIO()
    annotated_img.save(buf, format="JPEG", quality=95)
    img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    
    return f"""<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
    <body style="font-family:sans-serif; text-align:center; padding:20px; background:#222; margin:0;">
    <h3 style="color:white; margin-top:0;">解析完了</h3>
    <img src="data:image/jpeg;base64,{img_b64}" style="max-width:100%; height:auto; box-shadow: 0 4px 8px rgba(0,0,0,0.5); border-radius:8px;"><br><br>
    <a href="/" style="padding:12px 24px; font-size:16px; border-radius:5px; background:#007bff; color:white; text-decoration:none; display:inline-block;">もう一度解析する</a>
    </body></html>"""

if __name__ == "__main__": 
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
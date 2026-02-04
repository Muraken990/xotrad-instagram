#!/usr/bin/env python3
"""
Instagram Reels Auto-Generator for Xotrad
9秒の縦型リール動画を自動生成し、Instagram Reels APIで投稿する。

動画構成 (9.0秒、1080x1920):
    0.0-5.0s  商品画像×5 + ブランドロゴ×5 (各0.5秒、交互)
    5.0-9.0s  Xotradロゴ + テキストオーバーレイ (エンドカード)

Usage:
    python instagram-reels-generator.py                   # 自動でブランド選定＆投稿
    python instagram-reels-generator.py --brand Hermes     # 指定ブランド
    python instagram-reels-generator.py --dry-run          # 動画生成のみ（投稿しない）
    python instagram-reels-generator.py --list-brands      # 利用可能ブランド一覧

Requirements:
    pip install requests woocommerce
    ffmpeg (system)
"""

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime

import requests
from woocommerce import API

# === CONFIG ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

WOOCOMMERCE_CONFIG = {
    'url': 'https://xotrad.com',
    'consumer_key': 'ck_f1d34d5f51ab78f1865130dfbd2ec7796f443f44',
    'consumer_secret': 'cs_1b0cbb325b25c331c32ba8275b228747f24774f5',
}

INSTAGRAM_CONFIG = {
    'app_id': '839908195748501',
    'app_secret': '53921ed00c9ecaf80d58dafc3fc8c4a3',
    'user_id': '17841480619602761',
    'api_version': 'v21.0',
    'graph_url': 'https://graph.instagram.com',
}

SSH_CONFIG = {
    'host': 'ssh.lolipop.jp',
    'user': 'peewee.jp-soft-moji-2724',
    'port': 2222,
    'key': os.path.expanduser('~/.ssh/lolipop'),
    'remote_dir': 'web/xotrad/wp-content/uploads/reels',
    'base_url': 'https://xotrad.com/wp-content/uploads/reels',
}

# Paths
LOGOS_DIR = os.path.join(BASE_DIR, 'image-processor', 'logos')
PROCESSED_DIR = os.path.join(BASE_DIR, 'image-processor', 'processed')
XOTRAD_LOGO = os.path.join(BASE_DIR, 'sns-assets', 'xotrad-logo-800.png')
REELS_TEMP_DIR = os.path.join(BASE_DIR, 'reels', 'temp')
REELS_OUTPUT_DIR = os.path.join(BASE_DIR, 'reels', 'output')
POSTED_FILE = os.path.join(BASE_DIR, 'reels-posted.json')
TOKEN_FILE = os.path.join(BASE_DIR, 'instagram-token.json')

# Brand → logo filename mapping (actual files in image-processor/logos/)
BRAND_LOGO_MAP = {
    'Hermes':              'hermes.png',
    'Hermès':              'hermes.png',
    'Salvatore Ferragamo': 'ferragamo.png',
    'Ferragamo':           'ferragamo.png',
    'Gucci':               'gucci.png',
    'Burberry':            'burberry.png',
    'Dior':                'dior.png',
    'Christian Dior':      'dior.png',
    'Brioni':              'brioni.png',
    'Versace':             'versace.png',
    'Tom Ford':            'tom-ford.png',
    'Zegna':               'zegna.png',
    'Ermenegildo Zegna':   'zegna.png',
}

# Caption template
CAPTION_TEMPLATE = """\u2726 {brand} Collection | Authentic Pre-Owned Ties

\U0001f1ef\U0001f1f5 Direct From Japan
\U0001f4e6 Free Worldwide Shipping
\U0001f517 Shop now: xotrad.com

#{brand_hashtag} #luxurytie #silktie #designertie #mensfashion #xotrad #directfromjapan"""


# =============================================
# Token Management
# =============================================

def load_token():
    """保存済みアクセストークンを読み込む"""
    env_token = os.environ.get('INSTAGRAM_ACCESS_TOKEN')
    if env_token:
        return env_token
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'r') as f:
            data = json.load(f)
            return data.get('access_token')
    return None


# =============================================
# WooCommerce
# =============================================

def init_woocommerce():
    """WooCommerce API接続を初期化"""
    return API(
        url=WOOCOMMERCE_CONFIG['url'],
        consumer_key=WOOCOMMERCE_CONFIG['consumer_key'],
        consumer_secret=WOOCOMMERCE_CONFIG['consumer_secret'],
        version='wc/v3',
        timeout=120,
    )


def fetch_all_products(wcapi, per_page=100, max_pages=10):
    """WooCommerceから公開中の商品一覧を取得"""
    print("\n[1] WooCommerceから商品データ取得中...")
    all_products = []

    for page in range(1, max_pages + 1):
        params = {
            'per_page': per_page,
            'page': page,
            'status': 'publish',
            'stock_status': 'instock',
        }
        resp = wcapi.get('products', params=params)
        if resp.status_code != 200:
            print(f"  API エラー: {resp.status_code}")
            break

        products = resp.json()
        if not products:
            break

        all_products.extend(products)
        print(f"  ページ {page}: {len(products)}件取得")

        total_pages = int(resp.headers.get('X-WP-TotalPages', 1))
        if page >= total_pages:
            break

    print(f"  合計: {len(all_products)}件の商品を取得")
    return all_products


def extract_brand(wc_product):
    """WooCommerce商品からブランド名を抽出"""
    for attr in wc_product.get('attributes', []):
        if attr.get('name', '').lower() == 'brand':
            options = attr.get('options', [])
            if options:
                return options[0]
    return ''


def group_products_by_brand(products):
    """商品をブランド別にグループ化（画像が存在するもののみ）"""
    brand_groups = defaultdict(list)
    skipped = 0
    for p in products:
        brand = extract_brand(p)
        if brand and brand in BRAND_LOGO_MAP:
            sku = p.get('sku', '')
            if sku and get_product_image(sku):
                brand_groups[brand].append({
                    'id': p['id'],
                    'sku': sku,
                    'name': p.get('name', ''),
                    'brand': brand,
                })
            elif sku:
                skipped += 1
    if skipped:
        print(f"  画像なしスキップ: {skipped}件")
    return dict(brand_groups)


# =============================================
# Posted Record Management
# =============================================

def load_posted_records():
    """投稿済み記録を読み込む"""
    if os.path.exists(POSTED_FILE):
        with open(POSTED_FILE, 'r') as f:
            return json.load(f)
    return {'reels': []}


def save_posted_record(brand, skus, media_id, video_file):
    """投稿済み記録を保存"""
    records = load_posted_records()
    records['reels'].append({
        'brand': brand,
        'skus': skus,
        'media_id': media_id,
        'video_file': video_file,
        'posted_at': datetime.now().isoformat(),
    })
    with open(POSTED_FILE, 'w') as f:
        json.dump(records, f, indent=2, ensure_ascii=False)


def get_posted_skus():
    """投稿済みSKUのセットを取得"""
    records = load_posted_records()
    posted = set()
    for r in records.get('reels', []):
        for sku in r.get('skus', []):
            posted.add(sku)
    return posted


# =============================================
# Product Selection
# =============================================

def select_brand_and_products(brand_groups, posted_skus, target_brand=None):
    """ブランドを選定し、5商品をランダム選定（重複排除）"""
    if target_brand:
        # 指定ブランドを検索（大文字小文字無視）
        matched = None
        for brand in brand_groups:
            if brand.lower() == target_brand.lower():
                matched = brand
                break
        if not matched:
            print(f"  [!] ブランド '{target_brand}' が見つかりません")
            return None, []
        candidates = {matched: brand_groups[matched]}
    else:
        candidates = brand_groups

    # 未投稿商品が5件以上あるブランドを探す
    viable_brands = []
    for brand, products in candidates.items():
        unposted = [p for p in products if p['sku'] not in posted_skus]
        if len(unposted) >= 5:
            viable_brands.append((brand, unposted))

    if not viable_brands:
        # 5件未満でも最も多いブランドを選択
        for brand, products in candidates.items():
            unposted = [p for p in products if p['sku'] not in posted_skus]
            if len(unposted) > 0:
                viable_brands.append((brand, unposted))

    if not viable_brands:
        print("  [!] 未投稿商品がありません")
        return None, []

    # ランダムにブランドを選択（未投稿数が多いブランドを優先）
    viable_brands.sort(key=lambda x: len(x[1]), reverse=True)
    brand, unposted = viable_brands[0]

    # 5商品をランダム選定
    selected = random.sample(unposted, min(5, len(unposted)))

    return brand, selected


# =============================================
# FFmpeg Video Generation
# =============================================

def get_product_image(sku):
    """SKUから処理済み画像パスを取得（_1.jpg を使用）"""
    path = os.path.join(PROCESSED_DIR, f"{sku}_1.jpg")
    if os.path.exists(path):
        return path
    # PNG fallback
    path_png = os.path.join(PROCESSED_DIR, f"{sku}_1.png")
    if os.path.exists(path_png):
        return path_png
    return None


def get_brand_logo(brand):
    """ブランド名からロゴパスを取得"""
    logo_file = BRAND_LOGO_MAP.get(brand)
    if not logo_file:
        return None
    path = os.path.join(LOGOS_DIR, logo_file)
    if os.path.exists(path):
        return path
    return None


def run_ffmpeg(cmd):
    """FFmpegコマンドを実行"""
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        print(f"    FFmpeg error: {result.stderr[:300]}")
        return False
    return True


def generate_product_clip(image_path, clip_path, duration=2.0):
    """商品画像から1秒クリップを生成（ズームイン + フェードイン/アウト）"""
    fade_d = 0.2
    fps = 30
    total_frames = int(duration * fps)
    # ズーム: 1.0 → 1.08 にゆっくり拡大（Ken Burns効果）
    zoom_start = 1.0
    zoom_end = 1.02
    vf = (
        f"scale=4000:-1,"
        f"zoompan=z='zoom+{(zoom_end - zoom_start) / total_frames:.6f}'"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={total_frames}:s=1080x1080:fps={fps},"
        f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2:white,"
        f"fade=t=in:st=0:d={fade_d},fade=t=out:st={duration - fade_d}:d={fade_d}"
    )
    cmd = [
        'ffmpeg', '-y', '-loop', '1', '-i', image_path,
        '-t', str(duration),
        '-vf', vf,
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-r', str(fps),
        clip_path,
    ]
    return run_ffmpeg(cmd)


def generate_logo_clip(logo_path, clip_path, duration=0.8):
    """ブランドロゴから0.8秒クリップを生成（透過PNG対応、フェードイン/アウト付き）"""
    # 白背景を生成し、透過ロゴをオーバーレイ
    fade_d = 0.2
    vf = (
        f"color=white:s=1080x1920:d={duration}[bg];"
        f"[1:v]scale=700:-1[logo];"
        f"[bg][logo]overlay=(W-w)/2:(H-h)/2:format=auto,"
        f"fade=t=in:st=0:d={fade_d},fade=t=out:st={duration - fade_d}:d={fade_d}"
    )
    cmd = [
        'ffmpeg', '-y',
        '-f', 'lavfi', '-i', f'color=white:s=1080x1920:d={duration}:r=30',
        '-loop', '1', '-i', logo_path,
        '-t', str(duration),
        '-filter_complex',
        f'[1:v]scale=700:-1[logo];[0:v][logo]overlay=(W-w)/2:(H-h)/2:format=auto,'
        f'fade=t=in:st=0:d={fade_d},fade=t=out:st={duration - fade_d}:d={fade_d}',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-r', '30',
        '-shortest',
        clip_path,
    ]
    return run_ffmpeg(cmd)


def generate_endcard(clip_path, duration=3.0):
    """エンドカード（Xotradロゴ + テキスト、透過PNG対応、フェードイン付き）"""
    fade_d = 0.3
    cmd = [
        'ffmpeg', '-y',
        '-f', 'lavfi', '-i', f'color=black:s=1080x1920:d={duration}:r=30',
        '-loop', '1', '-i', XOTRAD_LOGO,
        '-t', str(duration),
        '-filter_complex',
        f'[1:v]scale=500:-1[logo];'
        f'[0:v][logo]overlay=(W-w)/2:(H-h)/2-150:format=auto,'
        f"drawtext=text='100%% Authentic'"
        f":fontsize=44:fontcolor=white"
        f":x=(w-text_w)/2:y=h/2+180,"
        f"drawtext=text='Free Worldwide Shipping'"
        f":fontsize=44:fontcolor=white"
        f":x=(w-text_w)/2:y=h/2+240,"
        f"drawtext=text='Direct From Japan'"
        f":fontsize=38:fontcolor=#C8A951"
        f":x=(w-text_w)/2:y=h/2+300,"
        f"drawtext=text='xotrad.com'"
        f":fontsize=32:fontcolor=#C8A951"
        f":x=(w-text_w)/2:y=h/2+360,"
        f'fade=t=in:st=0:d={fade_d}',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-r', '30',
        '-shortest',
        clip_path,
    ]
    return run_ffmpeg(cmd)


def generate_reel_video(brand, products):
    """
    リール動画を生成する。
    商品画像×5 (各1秒) + エンドカード (4秒) = 合計9秒
    Returns: output MP4 path or None on failure.
    """
    # Clean temp dir
    if os.path.exists(REELS_TEMP_DIR):
        shutil.rmtree(REELS_TEMP_DIR)
    os.makedirs(REELS_TEMP_DIR, exist_ok=True)

    clip_files = []
    clip_index = 0

    print(f"\n[2] FFmpegでクリップ生成中...")

    # Generate product image clips
    for i, product in enumerate(products):
        sku = product['sku']
        image_path = get_product_image(sku)

        if not image_path:
            print(f"    [!] 画像が見つかりません: {sku}")
            return None

        product_clip = os.path.join(REELS_TEMP_DIR, f"clip_{clip_index:02d}.mp4")
        print(f"    商品 {i+1}: {sku} → clip_{clip_index:02d}.mp4")
        if not generate_product_clip(image_path, product_clip):
            return None
        clip_files.append(product_clip)
        clip_index += 1

    # Endcard
    endcard_clip = os.path.join(REELS_TEMP_DIR, f"clip_{clip_index:02d}.mp4")
    print(f"    エンドカード → clip_{clip_index:02d}.mp4")
    if not generate_endcard(endcard_clip):
        return None
    clip_files.append(endcard_clip)

    # Create concat file list
    filelist_path = os.path.join(REELS_TEMP_DIR, 'filelist.txt')
    with open(filelist_path, 'w') as f:
        for clip in clip_files:
            f.write(f"file '{clip}'\n")

    # Concat all clips
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    brand_slug = brand.lower().replace(' ', '-').replace('è', 'e')
    output_filename = f"reel_{brand_slug}_{timestamp}.mp4"
    output_path = os.path.join(REELS_OUTPUT_DIR, output_filename)

    print(f"\n[3] クリップ結合中...")
    cmd = [
        'ffmpeg', '-y', '-f', 'concat', '-safe', '0',
        '-i', filelist_path,
        '-c', 'copy', '-movflags', '+faststart',
        output_path,
    ]
    if not run_ffmpeg(cmd):
        return None

    # Clean temp
    shutil.rmtree(REELS_TEMP_DIR, ignore_errors=True)

    # Verify output
    if os.path.exists(output_path):
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"  動画生成完了: {output_filename} ({size_mb:.1f} MB)")
        return output_path

    return None


# =============================================
# Upload via rsync
# =============================================

def upload_video(video_path):
    """rsyncで動画をxotrad.comにアップロード"""
    filename = os.path.basename(video_path)
    remote = f"{SSH_CONFIG['user']}@{SSH_CONFIG['host']}:{SSH_CONFIG['remote_dir']}/"

    print(f"\n[4] rsyncでアップロード中...")

    # Ensure remote directory exists
    mkdir_cmd = [
        'ssh',
        '-p', str(SSH_CONFIG['port']),
        '-i', SSH_CONFIG['key'],
        f"{SSH_CONFIG['user']}@{SSH_CONFIG['host']}",
        f"mkdir -p {SSH_CONFIG['remote_dir']}",
    ]
    subprocess.run(mkdir_cmd, capture_output=True, timeout=30)

    # rsync upload
    cmd = [
        'rsync', '-avz', '--progress',
        '-e', f"ssh -p {SSH_CONFIG['port']} -i {SSH_CONFIG['key']}",
        video_path,
        remote,
    ]
    print(f"  コマンド: {' '.join(cmd[:6])}...")

    result = subprocess.run(cmd, capture_output=False, text=True, timeout=120)

    if result.returncode == 0:
        public_url = f"{SSH_CONFIG['base_url']}/{filename}"
        print(f"  アップロード完了: {public_url}")
        return public_url
    else:
        print(f"  rsyncエラー: 終了コード {result.returncode}")
        return None


# =============================================
# Instagram Reels API
# =============================================

def create_reels_container(access_token, video_url, caption):
    """Instagram Reelsコンテナを作成"""
    url = (
        f"{INSTAGRAM_CONFIG['graph_url']}/{INSTAGRAM_CONFIG['api_version']}"
        f"/{INSTAGRAM_CONFIG['user_id']}/media"
    )
    payload = {
        'media_type': 'REELS',
        'video_url': video_url,
        'caption': caption,
        'share_to_feed': 'true',
        'access_token': access_token,
    }

    resp = requests.post(url, data=payload, timeout=60)
    if resp.status_code == 200:
        container_id = resp.json().get('id')
        print(f"    Reelsコンテナ作成: {container_id}")
        return container_id
    else:
        print(f"    コンテナ作成失敗: {resp.status_code}")
        print(f"    レスポンス: {resp.text}")
        return None


def check_container_status(access_token, container_id):
    """コンテナの処理状態を確認"""
    url = (
        f"{INSTAGRAM_CONFIG['graph_url']}/{INSTAGRAM_CONFIG['api_version']}"
        f"/{container_id}"
    )
    params = {
        'fields': 'status_code',
        'access_token': access_token,
    }
    resp = requests.get(url, params=params, timeout=30)
    if resp.status_code == 200:
        return resp.json().get('status_code', 'UNKNOWN')
    return 'ERROR'


def wait_for_container(access_token, container_id, max_attempts=20):
    """コンテナの処理完了を待つ（5秒間隔、最大20回）"""
    for attempt in range(max_attempts):
        time.sleep(5)
        status = check_container_status(access_token, container_id)
        if status == 'FINISHED':
            print(f"    コンテナ処理完了")
            return True
        elif status == 'ERROR':
            print(f"    コンテナ処理エラー")
            return False
        if attempt % 4 == 0 and attempt > 0:
            print(f"    状態: {status} (リトライ {attempt + 1}/{max_attempts})")
    print("    タイムアウト: コンテナ処理が完了しませんでした")
    return False


def publish_media(access_token, container_id):
    """メディアを公開"""
    url = (
        f"{INSTAGRAM_CONFIG['graph_url']}/{INSTAGRAM_CONFIG['api_version']}"
        f"/{INSTAGRAM_CONFIG['user_id']}/media_publish"
    )
    payload = {
        'creation_id': container_id,
        'access_token': access_token,
    }
    resp = requests.post(url, data=payload, timeout=60)
    if resp.status_code == 200:
        media_id = resp.json().get('id')
        print(f"    投稿公開成功! Media ID: {media_id}")
        return media_id
    else:
        print(f"    投稿公開失敗: {resp.status_code}")
        print(f"    レスポンス: {resp.text}")
        return None


def post_reel(access_token, video_url, caption):
    """Reelを投稿するフルフロー"""
    print(f"\n[5] Instagram Reels投稿中...")

    # Step 1: Create container
    container_id = create_reels_container(access_token, video_url, caption)
    if not container_id:
        return None

    # Step 2: Wait for processing
    print(f"    コンテナ処理待ち...")
    if not wait_for_container(access_token, container_id):
        return None

    # Step 3: Publish
    return publish_media(access_token, container_id)


# =============================================
# Caption Generation
# =============================================

def generate_caption(brand):
    """ブランド名からキャプションを生成"""
    import re
    brand_hashtag = re.sub(r'[^a-zA-Z0-9]', '', brand).lower()
    return CAPTION_TEMPLATE.format(
        brand=brand,
        brand_hashtag=brand_hashtag,
    )


# =============================================
# Main
# =============================================

def main():
    parser = argparse.ArgumentParser(description='Instagram Reels Generator for Xotrad')
    parser.add_argument('--brand', type=str, default='',
                        help='指定ブランドで生成')
    parser.add_argument('--dry-run', action='store_true',
                        help='動画生成のみ（投稿しない）')
    parser.add_argument('--list-brands', action='store_true',
                        help='利用可能ブランド一覧')
    parser.add_argument('--token', type=str, default='',
                        help='Instagram Access Token')
    args = parser.parse_args()

    print("=" * 50)
    print("Xotrad Instagram Reels Generator")
    print("=" * 50)

    # Ensure output dirs exist
    os.makedirs(REELS_TEMP_DIR, exist_ok=True)
    os.makedirs(REELS_OUTPUT_DIR, exist_ok=True)

    # Check ffmpeg
    if not shutil.which('ffmpeg'):
        print("[!] ffmpegが見つかりません。インストールしてください。")
        sys.exit(1)

    # Fetch products from WooCommerce
    wcapi = init_woocommerce()
    products_raw = fetch_all_products(wcapi)

    if not products_raw:
        print("[!] 商品が見つかりません")
        sys.exit(1)

    # Group by brand
    brand_groups = group_products_by_brand(products_raw)
    print(f"\n  ブランド数: {len(brand_groups)}")
    for brand, prods in sorted(brand_groups.items(), key=lambda x: -len(x[1])):
        print(f"    {brand}: {len(prods)}件")

    if args.list_brands:
        print("\n利用可能ブランド:")
        posted_skus = get_posted_skus()
        for brand, prods in sorted(brand_groups.items(), key=lambda x: -len(x[1])):
            unposted = [p for p in prods if p['sku'] not in posted_skus]
            print(f"  {brand}: {len(prods)}件 (未投稿: {len(unposted)}件)")
        sys.exit(0)

    # Select brand and products
    posted_skus = get_posted_skus()
    brand, selected = select_brand_and_products(
        brand_groups, posted_skus, target_brand=args.brand if args.brand else None
    )

    if not brand or not selected:
        print("\n[!] 投稿可能な商品がありません")
        sys.exit(1)

    print(f"\n  選定ブランド: {brand}")
    print(f"  選定商品: {len(selected)}件")
    for p in selected:
        print(f"    - {p['sku']}: {p['name'][:50]}")

    if len(selected) < 5:
        print(f"  [注意] 5件未満 ({len(selected)}件) で生成します")

    # Generate video
    video_path = generate_reel_video(brand, selected)

    if not video_path:
        print("\n[!] 動画生成に失敗しました")
        sys.exit(1)

    print(f"\n  出力: {video_path}")

    if args.dry_run:
        print(f"\n[DRY RUN] 投稿はスキップしました")
        print(f"  動画を確認してください: {video_path}")
        sys.exit(0)

    # Get access token
    access_token = args.token or load_token()
    if not access_token:
        print("\n[!] Instagram Access Tokenが見つかりません。")
        print("    --token YOUR_TOKEN で指定してください。")
        sys.exit(1)

    # Upload video via rsync
    video_url = upload_video(video_path)
    if not video_url:
        print("\n[!] 動画アップロードに失敗しました")
        sys.exit(1)

    # Post to Instagram
    caption = generate_caption(brand)
    media_id = post_reel(access_token, video_url, caption)

    if media_id:
        # Record posted
        skus = [p['sku'] for p in selected]
        save_posted_record(brand, skus, media_id, os.path.basename(video_path))
        print(f"\n{'=' * 50}")
        print(f"投稿完了!")
        print(f"  ブランド: {brand}")
        print(f"  商品数: {len(selected)}")
        print(f"  Media ID: {media_id}")
        print(f"{'=' * 50}")
    else:
        print(f"\n[!] Instagram投稿に失敗しました")
        sys.exit(1)


if __name__ == '__main__':
    main()

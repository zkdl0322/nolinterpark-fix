# -*- coding: utf-8 -*-

import io
import os
import re
import sys
import time
import threading
import traceback
import winsound

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
from PIL import Image
import numpy as np

from PySide6.QtCore import Qt, QThread, Signal, QObject
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QSpinBox, QPushButton,
    QButtonGroup, QTextEdit, QMessageBox
)

EXPIRE_DATE = "~26.09.30"
LOGIN_URL   = (
    "https://accounts.yanolja.com/v3/login"
    "?clientId=inpark-pc&postProc=FULLSCREEN"
    "&origin=https%3A%2F%2Fnol.interpark.com%2F"
    "&loginTab=NON_MEMBER_MY_BOOKING&service=interpark-integrate"
    "&nol_device_id=178123879502153851"
    "&redirect=aHR0cHM6Ly9hY2NvdW50cy5pbnRlcnBhcmsuY29tL2xvZ2luL3N1Y2Nlc3Mvbm9sP3Bvc3RQcm9jPUZVTExTQ1JFRU4mb3JpZ2luPWh0dHBzJTNBJTJGJTJGbm9sLmludGVycGFyay5jb20lMkY"
)
GRADES = ["스탠딩R", "스탠딩S", "지정석R", "지정석S", "지정석A", "지정석B"]  # fallback only

# 공연마다 등급/색상/구역이 다르므로 모두 페이지에서 동적으로 읽어온다.


# ───────────────────────────────────────────────
#  좌석배치도 이미지 → 구역 분리
# ───────────────────────────────────────────────
# 배치도를 '그림'으로 보고, 같은 색으로 이어진 덩어리 하나를 구역 하나로 본다.
# (구역 사이의 흰 경계선이 덩어리를 나눠 준다. svg/img/canvas 어떤 방식으로
#  그려졌든 화면에 보이는 그대로 읽으므로 DOM 구조와 무관하게 동작한다.)
# palette: 등급 범례 색 [[r,g,b], ...]  (없으면 유채색을 자동으로 묶는다)
# 반환: [{'color':[r,g,b], 'fx':0~1, 'fy':0~1, 'size':픽셀수, 'name':구역이름}, ...]
#       fx,fy = 이미지 안에서 그 구역 '안쪽 점'의 상대 위치(클릭용)
#       name  = 구역 안의 흰 글자를 읽은 이름(A, 207 ...). 못 읽으면 ''
def segment_zone_image(im, palette=None, tol=70, read_names=True):
    arr = np.asarray(im.convert("RGB")).astype(np.int32)
    H, W = arr.shape[:2]
    if H < 20 or W < 20:
        return []
    if palette:
        pal = np.array(palette, dtype=np.int32)                      # (P,3)
        d = np.abs(arr[:, :, None, :] - pal[None, None, :, :]).sum(axis=3)
        cls = d.argmin(axis=2)
        mask = d.min(axis=2) <= tol
        colors = [list(map(int, c)) for c in pal]
    else:
        mx, mn = arr.max(axis=2), arr.min(axis=2)
        mask = (mx - mn) > 45                                        # 유채색만
        q = arr // 40
        cls = q[:, :, 0] * 100 + q[:, :, 1] * 10 + q[:, :, 2]
        colors = None
    cls = np.where(mask, cls, -1)
    # 경계에서 3px 이상 떨어진 '안쪽' 픽셀 (클릭 지점 후보)
    inner = mask.copy()
    for dy, dx in ((3, 0), (-3, 0), (0, 3), (0, -3), (2, 2), (-2, -2), (2, -2), (-2, 2)):
        sh = np.zeros_like(mask)
        ys = slice(max(dy, 0), H + min(dy, 0)); yd = slice(max(-dy, 0), H + min(-dy, 0))
        xs = slice(max(dx, 0), W + min(dx, 0)); xd = slice(max(-dx, 0), W + min(-dx, 0))
        sh[yd, xd] = mask[ys, xs]
        inner &= sh
    grid = cls.tolist()
    inner_l = inner.tolist()
    minrgb = arr.min(axis=2)
    # 배경 밝기(구역이 아닌 무채색 픽셀의 중앙값) - 흰 글자와 배경을 구분하는 기준
    grey = (~mask) & ((arr.max(axis=2) - minrgb) < 25)
    bg_min = float(np.median(minrgb[grey])) if grey.any() else 255.0
    min_size = max(120, int(H * W * 0.0006))
    out = []
    for y0 in range(H):
        row = grid[y0]
        for x0 in range(W):
            c = row[x0]
            if c < 0:
                continue
            # 같은 색으로 이어진 영역 채우기
            row[x0] = -1
            stack = [(x0, y0)]
            pix = []
            while stack:
                x, y = stack.pop()
                pix.append((x, y))
                if x > 0 and grid[y][x - 1] == c:
                    grid[y][x - 1] = -1; stack.append((x - 1, y))
                if x < W - 1 and grid[y][x + 1] == c:
                    grid[y][x + 1] = -1; stack.append((x + 1, y))
                if y > 0 and grid[y - 1][x] == c:
                    grid[y - 1][x] = -1; stack.append((x, y - 1))
                if y < H - 1 and grid[y + 1][x] == c:
                    grid[y + 1][x] = -1; stack.append((x, y + 1))
            n = len(pix)
            if n < min_size:
                continue
            cx = sum(p[0] for p in pix) / n
            cy = sum(p[1] for p in pix) / n
            cand = [p for p in pix if inner_l[p[1]][p[0]]] or pix
            px, py = min(cand, key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)
            if colors is not None:
                col = colors[c]
            else:
                samp = pix[:: max(1, n // 200)]
                col = [int(sum(int(arr[b, a, k]) for a, b in samp) / len(samp))
                       for k in range(3)]
            name = ""
            if read_names:
                try:
                    name = _zone_label_from_pixels(pix, minrgb, min(col), bg_min)
                except Exception:
                    name = ""
            out.append({'color': col, 'fx': (px + 0.5) / W, 'fy': (py + 0.5) / H,
                        'cx': cx / W, 'cy': cy / H, 'size': n, 'name': name})
    # 위→아래, 왼→오 순
    out.sort(key=lambda z: (round(z['cy'] * 12), z['cx']))
    return out


# 구역(색 덩어리) 안에 '갇힌' 밝은 픽셀 = 구역 이름 글자. 그 부분만 떼어 읽는다.
def _zone_label_from_pixels(pix, minrgb, zone_min, bg_min=255):
    xs = [p[0] for p in pix]; ys = [p[1] for p in pix]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    w, h = x1 - x0 + 3, y1 - y0 + 3                      # 테두리 1px 여유
    comp = np.zeros((h, w), dtype=bool)
    comp[np.array(ys) - y0 + 1, np.array(xs) - x0 + 1] = True
    # 바깥에서 닿을 수 있는 곳을 채우고, 닿지 않는 곳(갇힌 곳)만 남긴다
    reach = np.zeros((h, w), dtype=bool)
    reach[0, :] = reach[-1, :] = reach[:, 0] = reach[:, -1] = True
    free = ~comp
    while True:
        grow = reach.copy()
        grow[1:, :] |= reach[:-1, :]; grow[:-1, :] |= reach[1:, :]
        grow[:, 1:] |= reach[:, :-1]; grow[:, :-1] |= reach[:, 1:]
        grow &= free
        if (grow == reach).all():
            break
        reach = grow
    enclosed = free & ~reach
    if enclosed.sum() < 12:
        return ""
    H, W = minrgb.shape
    sub = np.zeros((h, w), dtype=np.float64)
    ya, yb = max(0, y0 - 1), min(H, y1 + 2)
    xa, xb = max(0, x0 - 1), min(W, x1 + 2)
    sub[ya - (y0 - 1):yb - (y0 - 1), xa - (x0 - 1):xb - (x0 - 1)] = minrgb[ya:yb, xa:xb]
    # 글자가 구역 경계에 걸쳐 잘린 경우(일부만 갇힘)는 틀리게 읽히므로 포기한다.
    # = 구역 바로 바깥 테두리에 배경보다 뚜렷이 밝은(흰 글자) 픽셀이 있는 경우.
    # (배경 자체가 흰색인 배치도는 구분할 수 없으므로 이 검사를 생략)
    near = np.zeros((h, w), dtype=bool)
    near[1:, :] |= comp[:-1, :]; near[:-1, :] |= comp[1:, :]
    near[:, 1:] |= comp[:, :-1]; near[:, :-1] |= comp[:, 1:]
    edge = near & reach
    if bg_min < 248:
        n_white = int((edge & (sub >= bg_min + 0.4 * (255 - bg_min))).sum())
        if n_white >= 2:
            return ""
    white = np.clip((sub - zone_min) / max(1.0, 255.0 - zone_min), 0, 1)
    white[~enclosed] = 0
    return read_zone_label(white)


# ───────────────────────────────────────────────
#  구역 이름 읽기 (배치도 그림 속 흰 글자 → 문자)
# ───────────────────────────────────────────────
# 외부 OCR 프로그램 없이, 시스템 글꼴로 만든 글자 견본과 모양을 비교해 읽는다.
# 구역 이름은 보통 대문자 1글자(A) 또는 숫자 2~4자리(207)이며 회전돼 있기도 하다.
_OCR_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_OCR_FONTS = ["arialbd.ttf", "malgunbd.ttf", "segoeuib.ttf", "tahomabd.ttf",
              "FreeSansBold.ttf", "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf"]
_OCR_BOX = (20, 28)          # 글자 비교용 정규화 크기 (w, h)
_ocr_templates = None
# NOL 인터파크 배치도 글꼴에서 떼어 낸 글자 견본 (20x28 비트맵, 16진수)
_OCR_SITE_TEMPLATES = {
    '0': (0.778, '01f8007fe00fff01fff83fffc7f0fe7e07e7c03e7c03efc03ffc03ffc03ff801ff801ff801ff801ffc03ffc03ffc03f7c03e7c03e7e07e3f0fe3fffc1fff80fff007fe001f80'),
    '1': (0.472, '003fc00ffe07ffe1fffe3fffeffffefe3fefc3fee03fe003fe003fe003fe003fe003fe001fe001fe001fe001fe001fe001ff001ff001ff001ff001ff001ff001ff001ff000fc'),
    '2': (0.718, '03fc00fff01fff83fffc7fffe7e07efc03ffc03f7803f0003f0003e0007e000fc001fc003f8007f000fe001fc003f8007f001fe001fc003f800ffffffffffffffffffff7fffe'),
    '3': (0.741, '01f800ffe01fff83fff87fffc7e0fe7c07e7803e0003e0007e0007c003f801ff001ff001ff801ffc000fe0003e0003f0003ff803ffc03ffe07f7fffe7fffc3fff80fff003fc0'),
    '4': (0.815, '003f0007f0007f000ff000ff001ff001ff803ff803cf807cf8078f80f8f81f0f81f0f81e0f83e0f87c0f87c0f8fc1fcffffffffffffffffffff001f8000f8000f8000f8000f0'),
    '5': (0.741, '3fffe3fffe3fffe3fffc3fffc3e0003e0003e0003c0003c7c07fff07fff87fffc7f1fe7e07e7c03f0003f0001f0001f7001ffc03ffc03ffe07e7fffe3fffc1fff80ffe001fc0'),
    '6': (0.778, '01fc007ff00fff81fffc3fffe3f07e7e03e7e01efc000fc380fcff0fdff8ffffcffffeff0fefe07efc03ffc03ffc03ffc03f7c03e7e03f7e07e3fffc1fffc0fff807ff001fc0'),
    '7': (0.694, '7fffffffffffffffffff7ffff0003f0007e0007e000fc000fc001f8003f8003f0003e0007e000fe000fc001f8001f8003f0003f0007f0007e000fc000fc001f8001f0003f000'),
    '8': (0.753, '01f8007fe01fff83fff83fbfc7e07e7e07e7c03e7c03e7e07e3e07c1fff80fff007fe01fff83fff87e07e7c03efc03efc03ffc03ffc03ffe07e7fffe3fffc1fff80fff003fc0'),
    '9': (0.747, '01f000ffe01fff03fff87fffc7e0fefc07efc03ef803ef803ff803ffc03ffc07f7e0ff7ffff3ffff1ffbf07c1f0003e0003e7803efc07e7e1fc7fff83fff81fff00ffc003f00'),
    'A': (0.917, '00f8001f8001f8003fc003fc003fe003fe007fe007de0079f00f9f00f0f00f0f00f0f00f0f81f0f81e0f83fffc3fffc3fffc3fffc7fffc7c03e7c01e7801ef801ff801ff800f'),
    'B': (0.778, '7ffc07fff0ffff8ffffcffffefc0fefc07efc03efc03efc03efc07cfc0fcffff0ffff0ffff8ffffcfc07efc03ffc03ffc03ffc03ffc03ffc07ffffffffffeffffcffff8ffff0'),
    'C': (0.865, '01fc003ff00fff80fffc1fffc3f87e7e03f7e01f7c01f7c00f78000f8000f8000f8000f8000f8000f8000780007c00f7c01f7c01f7e03f3f07e1fffc0fffc07ff803fe000700'),
    'D': (0.852, 'fff00fffc0fffe0ffff0ffff8f80f8f807cf803ef803ef803ef801ef801ff801ff801ff801ff801ff801ff801ef803ef803ef807ef80fcfc3f8ffff0ffff0fffe0fff007fc00'),
    'E': (0.667, 'ffffffffffffffffffffffffffc000fc000fc000fc000fc000fc000ffffcffffcffffcffffcffffcfe000fc000fc000fc000fc000fc000fe000ffffffffffffffffffff7fffe'),
    'F': (0.667, 'ffffffffffffffffffffffffffe000fe000fe000fe000fe000fe000ffffeffffeffffeffffeffffeff000fe000fe000fe000fe000fe000fe000fe000fe000fe000fc0007c000'),
    'G': (0.889, '03fc007ff00fff81fffc3fffc3f07e7c03e7c03e7c01e78000f8000f0000f00fef01fff01fff01fff01fff801f7800f7c01f7c03e7c03e3f07e3fffc1fffc0fff807ff001fc0'),
    'H': (0.861, 'f801ff801ff801ff801ff801ff801ff801ff801ff801ff801ff801ffc01ffffffffffffffffffffffc01ff801ff801ff801ff801ff801ff801ff801ff801ff801ff801ff801f'),
    'I': (0.222, 'ffffcffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffc'),
}


def _ocr_norm(mask):
    # 글자 하나의 2진 마스크 → (고정 크기로 늘린 모양, 가로/세로 비율)
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    m = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = m.shape
    im = Image.fromarray((m * 255).astype(np.uint8)).resize(_OCR_BOX, Image.BILINEAR)
    return (np.asarray(im) > 127, w / float(h))


def _ocr_load_templates():
    global _ocr_templates
    if _ocr_templates is not None:
        return _ocr_templates
    from PIL import ImageDraw, ImageFont
    tpl = []                                   # [(char, mask, aspect), ...]
    bw, bh = _OCR_BOX
    for ch, (asp, hx) in _OCR_SITE_TEMPLATES.items():
        bits = bin(int(hx, 16))[2:].zfill(bw * bh)
        tpl.append((ch, np.array([b == '1' for b in bits]).reshape(bh, bw), asp))
    for name in _OCR_FONTS:
        try:
            font = ImageFont.truetype(name, 64)
        except Exception:
            continue
        for ch in _OCR_CHARS:
            im = Image.new("L", (120, 120), 0)
            ImageDraw.Draw(im).text((20, 20), ch, fill=255, font=font)
            n = _ocr_norm(np.asarray(im) > 127)
            if n is not None:
                tpl.append((ch, n[0], n[1]))
    _ocr_templates = tpl
    return tpl


def _ocr_char(mask, allowed):
    n = _ocr_norm(mask)
    if n is None:
        return None, 0.0
    shape, asp = n
    best, score = None, 0.0
    for ch, t, tasp in _ocr_load_templates():
        if ch not in allowed:
            continue
        inter = np.logical_and(shape, t).sum()
        union = np.logical_or(shape, t).sum()
        s = inter / float(union) if union else 0.0
        s -= 0.35 * abs(np.log(asp / tasp))        # 폭이 다른 글자(1 과 4 등) 구분
        if s > score:
            best, score = ch, s
    return best, score


def _ocr_line(mask):
    # 가로로 놓인 글자줄 마스크 → (문자열, 평균 점수)
    ys, xs = np.nonzero(mask)
    if len(xs) < 20:
        return "", 0.0
    mask = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h = mask.shape[0]
    cols = mask.sum(axis=0) > 0
    segs, start = [], None
    for x, on in enumerate(list(cols) + [False]):
        if on and start is None:
            start = x
        elif not on and start is not None:
            if x - start >= max(2, h // 12):
                segs.append((start, x))
            start = None
    if not segs or len(segs) > 4:
        return "", 0.0
    digits, letters = "0123456789", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    text, total = "", 0.0
    for i, (a, b) in enumerate(segs):
        if len(segs) == 1:
            ch, sc = _ocr_char(mask[:, a:b], letters)          # 한 글자 구역 = 알파벳
            ch2, sc2 = _ocr_char(mask[:, a:b], digits)
            if sc2 > sc + 0.12:
                ch, sc = ch2, sc2
        else:
            ch, sc = _ocr_char(mask[:, a:b], digits)           # 여러 글자 = 숫자
            if i == 0:
                ch2, sc2 = _ocr_char(mask[:, a:b], letters)    # 맨 앞만 알파벳 허용(A12)
                if sc2 > sc + 0.12:
                    ch, sc = ch2, sc2
        if not ch:
            return "", 0.0
        text += ch
        total += sc
    return text, total / len(segs)


# white: 구역 bbox 크기의 0~1 배열(글자일수록 1). 반환: 구역 이름 또는 ''
def read_zone_label(white):
    if white.max() <= 0:
        return ""
    ys, xs = np.nonzero(white > 0.5)
    if len(xs) < 12:
        return ""
    # 글자줄의 기울기(주축) 추정
    x = xs - xs.mean(); y = ys - ys.mean()
    cov = np.array([[np.mean(x * x), np.mean(x * y)], [np.mean(x * y), np.mean(y * y)]])
    vals, vecs = np.linalg.eigh(cov)
    major = vecs[:, 1]
    ang = float(np.degrees(np.arctan2(major[1], major[0])))
    elong = (vals[1] / vals[0]) if vals[0] > 1e-6 else 99.0
    angles = [0.0]
    if elong > 1.25:
        angles += [ang, ang + 180.0]
    im = Image.fromarray((np.clip(white, 0, 1) * 255).astype(np.uint8))
    scale = max(1, int(round(120.0 / max(im.size))))            # 작은 글자는 키워서
    scale = min(scale, 6)
    if scale > 1:
        im = im.resize((im.width * scale, im.height * scale), Image.BICUBIC)
    best, bscore = "", 0.0
    for a in angles:
        r = im if abs(a) < 0.5 else im.rotate(a, resample=Image.BICUBIC, expand=True)
        text, sc = _ocr_line(np.asarray(r) > 127)
        if a == 0.0:
            sc += 0.03                                           # 동점이면 똑바른 쪽
        if text and sc > bscore:
            best, bscore = text, sc
    return best if bscore >= 0.5 else ""


# ───────────────────────────────────────────────
#  좌석 화면(확대된 배치도) 이미지 → 예매 가능 좌석 찾기
# ───────────────────────────────────────────────
# 예매 가능한 좌석은 등급 색이 칠해진 작은 동그라미, 매진 좌석은 회색이다.
# 색이 있는 작은 덩어리 중 동그라미 모양·비슷한 크기인 것만 좌석으로 본다.
# target: 선택 등급 색 [r,g,b] (None 이면 색 있는 좌석 전부)
# 반환: [{'fx':0~1, 'fy':0~1, 'color':[r,g,b]}, ...]
def _hue(c):
    r, g, b = [v / 255.0 for v in c[:3]]
    mx, mn = max(r, g, b), min(r, g, b)
    if mx - mn < 1e-6:
        return 0.0
    if mx == r:
        h = ((g - b) / (mx - mn)) % 6
    elif mx == g:
        h = (b - r) / (mx - mn) + 2
    else:
        h = (r - g) / (mx - mn) + 4
    return h * 60.0


def find_available_seats(im, target=None, hue_tol=28.0):
    arr = np.asarray(im.convert("RGB")).astype(np.int32)
    H, W = arr.shape[:2]
    if H < 20 or W < 20:
        return []
    sat = arr.max(axis=2) - arr.min(axis=2)
    mask = sat >= 38
    grid = mask.tolist()
    comps = []
    max_side = max(12, int(min(H, W) * 0.09))
    for y0 in range(H):
        row = grid[y0]
        for x0 in range(W):
            if not row[x0]:
                continue
            row[x0] = False
            stack = [(x0, y0)]
            n = 0; sx = sy = 0
            xa = xb = x0; ya = yb = y0
            best_s, best_c = -1, None
            while stack:
                x, y = stack.pop()
                n += 1; sx += x; sy += y
                if x < xa: xa = x
                if x > xb: xb = x
                if y < ya: ya = y
                if y > yb: yb = y
                s = sat[y, x]
                if s > best_s:
                    best_s, best_c = s, (x, y)
                if x > 0 and grid[y][x - 1]:
                    grid[y][x - 1] = False; stack.append((x - 1, y))
                if x < W - 1 and grid[y][x + 1]:
                    grid[y][x + 1] = False; stack.append((x + 1, y))
                if y > 0 and grid[y - 1][x]:
                    grid[y - 1][x] = False; stack.append((x, y - 1))
                if y < H - 1 and grid[y + 1][x]:
                    grid[y + 1][x] = False; stack.append((x, y + 1))
            w, h = xb - xa + 1, yb - ya + 1
            if w < 5 or h < 5 or w > max_side or h > max_side:
                continue
            if not (0.75 <= w / float(h) <= 1.34):
                continue                                   # 동그라미(정사각 bbox)만
            if n < 0.3 * w * h:
                continue
            if xa <= 0 or ya <= 0 or xb >= W - 1 or yb >= H - 1:
                continue                                   # 화면 가장자리에 잘린 것
            bx, by = best_c
            comps.append({'cx': sx / float(n), 'cy': sy / float(n), 'side': (w + h) / 2.0,
                          'color': [int(v) for v in arr[by, bx]]})
    if not comps:
        return []
    # 좌석은 크기가 서로 같다 → 가장 흔한 크기에서 벗어난 것(아이콘, 글자 등) 제외
    sides = sorted(c['side'] for c in comps)
    med = sides[len(sides) // 2]
    comps = [c for c in comps if 0.7 * med <= c['side'] <= 1.4 * med]
    if target:
        th = _hue(target)
        keep = []
        for c in comps:
            d = abs(_hue(c['color']) - th) % 360.0
            if min(d, 360.0 - d) <= hue_tol:
                keep.append(c)
        comps = keep
    return [{'fx': c['cx'] / W, 'fy': c['cy'] / H, 'color': c['color']} for c in comps]


# ───────────────────────────────────────────────
#  신호 브릿지
# ───────────────────────────────────────────────
class Signals(QObject):
    log_signal   = Signal(str)
    show_input   = Signal()
    hide_input   = Signal()


# ───────────────────────────────────────────────
#  매크로 스레드
# ───────────────────────────────────────────────
class MacroThread(QThread):

    def __init__(self, driver, delay, signals):
        super().__init__()
        self.driver  = driver
        self.delay   = delay
        self.sig     = signals
        self._stop   = False
        self._pause  = False
        self._ev     = threading.Event()
        self._answer = ""

    def stop(self):
        self._stop = True

    def toggle_pause(self):
        self._pause = not self._pause
        return self._pause

    def set_answer(self, text):
        self._answer = text
        self._ev.set()

    # ── 로그 ──────────────────────────────────
    def log(self, msg):
        ts = time.strftime("%Y/%m/%d %H:%M:%S")
        self.sig.log_signal.emit(f"[{ts}] {msg}")

    # ── 대기 ──────────────────────────────────
    def _wait(self, sec):
        # sec=0 이어도 정지/일시정지를 최소 한 번은 확인하고 빠져나간다.
        end = time.time() + sec
        while True:
            if self._stop: raise InterruptedError
            while self._pause:
                if self._stop: raise InterruptedError
                time.sleep(0.1)
            if time.time() >= end:
                return
            time.sleep(0.05)

    # ── GUI 입력 요청 ─────────────────────────
    def _ask(self, timeout=120):
        self._ev.clear()
        self.sig.show_input.emit()
        ok  = self._ev.wait(timeout=timeout)
        ans = self._answer
        self.sig.hide_input.emit()
        return ans if ok else None

    # ── URL / 소스 헬퍼 ──────────────────────
    def _url(self):
        try: return self.driver.current_url
        except: return ""

    def _src(self):
        try: return self.driver.page_source
        except: return ""

    # ── 로그인 완료 감지 ──────────────────────
    # 로그인 페이지(accounts.*)를 벗어나 서비스 도메인(nol/poticket interpark,
    # nol.yanolja, myaccount)에 도달하면 로그인 완료로 간주한다.
    def _is_logged_in(self):
        url = self._url()
        if not url:
            return False
        # 아직 로그인/계정 인증 페이지에 있으면 미완료
        if "accounts.yanolja.com" in url or "accounts.interpark.com" in url:
            return False
        return (
            "nol.interpark.com" in url
            or "poticket.interpark.com" in url
            or "ticket.interpark.com" in url
            or "nol.yanolja.com" in url
            or "myaccount" in url
        )

    # ── 예매창 찾기 ───────────────────────────
    # 열려 있는 모든 탭의 주소를 (탭 전환 없이) 조회해 예매창을 찾고,
    # 찾았을 때만 그 탭으로 한 번 전환한다. 현재 탭이 닫혔거나, 예매창이
    # 어느 탭에서 열렸든 상관없이 동작한다.
    def _switch_to_booking_window(self, kws):
        drv = self.driver
        # 1) 현재 탭이 이미 예매창
        try:
            if any(k in drv.current_url for k in kws):
                return True
        except Exception:
            pass   # 현재 탭이 닫힌 경우 등
        try:
            handles = drv.window_handles
        except Exception:
            return False
        # 2) CDP로 전체 탭 주소 조회 (탭을 건드리지 않음)
        try:
            infos = drv.execute_cdp_cmd("Target.getTargets", {}).get("targetInfos", [])
            for t in infos:
                if t.get("type") != "page":
                    continue
                if any(k in (t.get("url") or "") for k in kws):
                    tid = t.get("targetId")
                    if tid in handles:
                        drv.switch_to.window(tid)
                        self._wait(1.0)
                        return True
                    break   # 핸들 형식이 다르면 아래 3)으로 찾는다
            else:
                # 예매창이 아직 없음. 현재 탭이 닫혔다면 살아있는 탭으로 옮겨 둔다.
                try:
                    drv.current_url
                except Exception:
                    if handles:
                        drv.switch_to.window(handles[-1])
                return False
        except Exception:
            pass
        # 3) 폴백: 탭을 하나씩 전환하며 확인 (최근 탭부터)
        for h in reversed(handles):
            try:
                drv.switch_to.window(h)
                if any(k in drv.current_url for k in kws):
                    self._wait(1.0)
                    return True
            except Exception:
                continue
        return False

    # ── 안심예매 보안문자(캡챠) 감지 ───────────
    # poticket BookMain.asp 는 중첩 iframe 구조라 모든 프레임을 재귀로 훑는다.
    # 보이는 캡챠 입력창 또는 안심예매 안내문이 발견되면 True.
    def _captcha_present(self):
        drv = self.driver
        try:
            drv.switch_to.default_content()
        except Exception:
            return False
        found = self._scan_captcha(0)
        try:
            drv.switch_to.default_content()
        except Exception:
            pass
        return found

    def _scan_captcha(self, depth):
        drv = self.driver
        # 1) 현재 프레임에서 캡챠 입력창 탐지 (넓은 셀렉터)
        input_sels = [
            "input[placeholder*='문자']",
            "input[id*='aptcha']",
            "input[name*='aptcha']",
            "#txtCaptcha", "#captchaInput",
            ".captcha_input input", ".capchaInner input",
        ]
        for sel in input_sels:
            try:
                for el in drv.find_elements(By.CSS_SELECTOR, sel):
                    if el.is_displayed():
                        return True
            except Exception:
                pass
        # 2) 텍스트 기반 (안심예매 보안문자 안내문)
        try:
            src = drv.page_source
            if "부정예매방지" in src and "문자를 입력" in src:
                return True
        except Exception:
            pass
        if depth >= 3:
            return False
        # 3) 하위 iframe 재귀 탐색
        try:
            cnt = len(drv.find_elements(By.TAG_NAME, "iframe"))
        except Exception:
            cnt = 0
        for i in range(cnt):
            try:
                frames = drv.find_elements(By.TAG_NAME, "iframe")
                if i >= len(frames):
                    break
                drv.switch_to.frame(frames[i])
                if self._scan_captcha(depth + 1):
                    return True
                drv.switch_to.parent_frame()
            except Exception:
                try:
                    drv.switch_to.parent_frame()
                except Exception:
                    try: drv.switch_to.default_content()
                    except Exception: pass
        return False

    # ── 안심예매 보안문자 통과 대기 ────────────
    # 자동 입력은 중첩 iframe/캡챠 변형에 취약하므로, 사용자가 예매창에서
    # 직접 보안문자를 입력하도록 안내하고 캡챠가 사라질 때까지 대기한다.
    def _wait_captcha_cleared(self, timeout=300):
        self.log("안심예매 보안문자가 떴습니다.")
        self.log("→ 예매창에서 직접 보안문자를 입력하고 [입력완료]를 눌러주세요.")
        self.log("  통과되면 자동으로 다음 단계로 진행합니다...")
        end = time.time() + timeout
        while time.time() < end:
            self._wait(1.0)
            if not self._captcha_present():
                self.log("→ 보안문자 통과 확인")
                return True
        self.log("보안문자 대기 시간 초과")
        return False

    # ── 텍스트 버튼 클릭 헬퍼 ─────────────────
    # labels 중 화면에 보이는 첫 요소를 클릭. 클릭한 라벨 반환(없으면 None)
    def _click_text_button(self, labels):
        drv = self.driver
        js = r"""
        var labels = arguments[0];
        var nodes = document.querySelectorAll('a,button,span,div,li,p');
        for (var i=0;i<nodes.length;i++){
            var t=(nodes[i].textContent||'').replace(/\s+/g,'').trim();
            for (var k=0;k<labels.length;k++){
                if (t===labels[k]){
                    var b=nodes[i].getBoundingClientRect();
                    if (b.width>0 && b.height>0){ nodes[i].click(); return labels[k]; }
                }
            }
        }
        return null;
        """
        try: return drv.execute_script(js, labels)
        except: return None

    # ── 좌석가격(등급) 패널 열기 ─────────────
    def _open_price_panel(self):
        # '가격 전체보기' 패널을 연다(등급별 구역 링크가 나열됨). 모든 프레임 시도.
        for xp in [
            # 신형 onestop 좌석 페이지: 좌측 하단 '등급 별 가격' 버튼
            "//*[contains(normalize-space(text()),'등급 별 가격')]",
            "//*[contains(normalize-space(text()),'등급별 가격')]",
            "//*[contains(text(),'가격 전체보기')]",
            "//*[contains(text(),'가격전체보기')]",
            "//*[contains(text(),'좌석가격보기')]",
            "//*[contains(text(),'가격보기')]",
        ]:
            if self._open_price_panel_recursive(xp, 0):
                return True
        return False

    def _open_price_panel_recursive(self, xp, depth):
        drv = self.driver
        try:
            for el in drv.find_elements(By.XPATH, xp):
                try:
                    if el.is_displayed():
                        drv.execute_script("arguments[0].click();", el)
                        self._wait(0.6)
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        if depth >= 3:
            return False
        try:
            cnt = len(drv.find_elements(By.TAG_NAME, "iframe"))
        except Exception:
            cnt = 0
        for i in range(cnt):
            try:
                frames = drv.find_elements(By.TAG_NAME, "iframe")
                if i >= len(frames):
                    break
                drv.switch_to.frame(frames[i])
                if self._open_price_panel_recursive(xp, depth + 1):
                    return True
                drv.switch_to.parent_frame()
            except Exception:
                try: drv.switch_to.parent_frame()
                except Exception:
                    try: drv.switch_to.default_content()
                    except Exception: pass
        return False

    # ── 좌석가격(등급) 패널 닫기 ─────────────
    # 가격표를 읽은 뒤 '가격닫기'를 눌러 좌석배치도로 복귀시킨다.
    def _close_price_panel(self):
        # 1) '가격닫기' 버튼 클릭 (가격표 열려있을 때만 존재)
        if self._click_text_button(["가격닫기"]):
            self._wait(0.4)
            return True
        # 1-2) 신형 onestop: '등급 별 가격' 팝업은 다시 누르면 닫힌다(토글)
        if self._is_onestop():
            js = r"""
            var nodes=document.querySelectorAll('div,ul,section,aside');
            for(var i=0;i<nodes.length;i++){
                var t=(nodes[i].textContent||'');
                if(/\d[\d,]*\s*원/.test(t) && /석/.test(t) && t.length<300
                   && nodes[i].getBoundingClientRect().width>0) return true;
            }
            return false;
            """
            try:
                opened = bool(self.driver.execute_script(js))
            except Exception:
                opened = False
            if opened and self._click_text_button(["등급별가격"]):
                self._wait(0.4)
                return True
            return False
        # 2) JS로 가격 패널 요소 직접 숨김 (폴백)
        js = r"""
        var sels=['.price_view','.seat_price','[class*="priceView"]',
                  '[class*="seatPrice"]','[class*="price_layer"]','[class*="legend"]'];
        var hidden=false;
        for(var s=0;s<sels.length;s++){
            var els=document.querySelectorAll(sels[s]);
            for(var i=0;i<els.length;i++){
                els[i].style.display='none'; hidden=true;
            }
        }
        return hidden;
        """
        try: return bool(self.driver.execute_script(js))
        except: return False

    # ── 잔여좌석 안내 패널 닫기 (좌석 잡은 뒤) ─
    # 좌석 선택 후 뜨는 '잔여좌석 안내' 패널을 '좌석닫기'로 닫는다.
    def _close_seat_panel(self):
        if self._click_text_button(["좌석닫기"]):
            self._wait(0.4)
            return True
        return False

    # ── 모든 프레임 재귀 실행 헬퍼 ────────────
    # 현재 컨텍스트부터 하위 iframe까지 재귀로 fn()을 실행해 결과 리스트를 누적.
    # (poticket BookMain.asp 처럼 좌석/등급이 중첩 iframe 안에 있을 때 사용)
    def _collect_all_frames(self, fn, depth=0):
        drv = self.driver
        try:
            out = list(fn() or [])
        except Exception:
            out = []
        if depth >= 4:
            return out
        try:
            cnt = len(drv.find_elements(By.TAG_NAME, "iframe"))
        except Exception:
            cnt = 0
        for i in range(cnt):
            try:
                frames = drv.find_elements(By.TAG_NAME, "iframe")
                if i >= len(frames):
                    break
                drv.switch_to.frame(frames[i])
                out += self._collect_all_frames(fn, depth + 1)
                drv.switch_to.parent_frame()
            except Exception:
                try:
                    drv.switch_to.parent_frame()
                except Exception:
                    try: drv.switch_to.default_content()
                    except Exception: pass
        return out

    # ── 등급 목록 + 색상 동적 읽기 ────────────
    # 가격 패널을 열지 않고 현재 DOM에서 등급 행을 스캔한다.
    def _scan_grades(self):
        js = r"""
        var out = [], seen = {};
        var all = document.querySelectorAll('li,tr,div,p,span,dt,dd,td');
        for (var i=0;i<all.length;i++){
            var el = all[i];
            var txt = (el.textContent||'').replace(/\s+/g,' ').trim();
            if (txt.length>40) continue;
            if (!/석|존/.test(txt)) continue;        // 등급명에 '석' 또는 '존'
            if (!/\d[\d,]*\s*원/.test(txt)) continue; // 가격(숫자+원) 포함 행
            var name = txt.replace(/[\d,]+\s*원.*/,'').replace(/\d+\s*석/g,'').replace(/잔여|매진/g,'').replace(/\s+/g,' ').trim();
            if (!name || name.length<2 || seen[name]) continue;
            // '등급 별 가격' 버튼 문구가 섞인 행, 안내 문구 제외
            if (/가격|선택해/.test(name)) continue;
            // 색상 swatch(등급 색 점) 찾기: 회색 구분선/배경이 아닌 '유채색'만 인정.
            // 배경색, SVG fill, ::before/::after 를 모두 보고, 행 안에 없으면
            // 같은 등급 행으로 보이는 상위 요소까지 올라가며 찾는다.
            function sat(s){
                var m=(s||'').match(/(\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?/);
                if(!m) return null;
                if(m[4]!==undefined && +m[4]===0) return null;
                var r=+m[1],g=+m[2],b=+m[3];
                if(Math.max(r,g,b)-Math.min(r,g,b)<40) return null;   // 흰/회/검정
                return [r,g,b];
            }
            function swatch(root){
                var scan=[root].concat([].slice.call(root.querySelectorAll('*')));
                for(var j=0;j<scan.length;j++){
                    var e=scan[j], c=null;
                    try{
                        var cs=getComputedStyle(e);
                        c=sat(cs.backgroundColor);
                        if(!c && e instanceof SVGElement) c=sat(cs.fill);
                        if(!c) c=sat(getComputedStyle(e,'::before').backgroundColor);
                        if(!c) c=sat(getComputedStyle(e,'::after').backgroundColor);
                    }catch(err){}
                    if(c) return c;
                }
                return null;
            }
            var color = swatch(el);
            var up = el.parentElement;
            for(var d=0; !color && up && d<4; d++){
                var pt=(up.textContent||'').replace(/\s+/g,' ').trim();
                if(pt.length>40 || (pt.match(/\d[\d,]*\s*원/g)||[]).length!==1) break;
                color = swatch(up);
                up = up.parentElement;
            }
            seen[name]=1;
            out.push({name:name, color:color});
        }
        return out;
        """
        try:
            return self.driver.execute_script(js) or []
        except:
            return []

    # ── 등급 목록 읽기 (모든 프레임 재귀 스캔) ──
    def _dedup_grades(self, rows):
        seen, out = set(), []
        for r in rows:
            n = (r or {}).get('name')
            if n and n not in seen:
                seen.add(n); out.append(r)
        return out

    def _get_grades(self):
        drv = self.driver
        try: drv.switch_to.default_content()
        except Exception: pass
        rows = self._dedup_grades(self._collect_all_frames(self._scan_grades))
        try: drv.switch_to.default_content()
        except Exception: pass
        if rows:
            return rows
        # 폴백: 가격 패널을 열어 다시 시도
        self._open_price_panel()
        self._wait(0.4)
        try: drv.switch_to.default_content()
        except Exception: pass
        rows = self._dedup_grades(self._collect_all_frames(self._scan_grades))
        try: drv.switch_to.default_content()
        except Exception: pass
        self._close_price_panel()
        return rows

    # ── 좌석 등급 선택 ───────────────────────
    def _ask_grade(self):
        grades = self._get_grades()
        if not grades:
            self.log("등급 정보를 읽지 못했습니다 → 모두로 진행")
            return None, []
        self.log("좌석 등급을 입력해주세요:")
        self.log("1. 모두")
        for i, g in enumerate(grades, 2):
            self.log(f"{i}. {g['name']}")
        ans = self._ask(timeout=120)
        if ans is None:
            self.log("시간 초과 → 모두로 진행"); return None, grades
        self.log(f"→ {ans}")
        try: n = int(ans)
        except: return None, grades
        if n == 1: return None, grades
        if 2 <= n <= len(grades) + 1:
            sel = grades[n - 2]
            self.log(f"선택 등급: {sel['name']}")
            return sel, grades
        return None, grades

    # ── iframe 전환 헬퍼 ──────────────────────
    def _to_frame(self, *ids):
        self.driver.switch_to.default_content()
        for fid in ids:
            try:
                self.driver.switch_to.frame(
                    self.driver.find_element(By.ID, fid)
                )
                return True
            except: pass
        return False

    # ── 구역 목록 동적 읽기 (label + color) ─────
    # 반환: [{'label': '101', 'color': [r,g,b] or None}, ...]
    # poticket BookMain.asp 는 구역이 중첩 iframe(이미지맵 area 태그 또는
    # SVG/HTML) 안에 있으므로 모든 프레임을 재귀로 훑는다.
    def _get_zones(self):
        drv = self.driver
        try: drv.switch_to.default_content()
        except Exception: pass
        allz = self._collect_all_frames(self._scan_zones_current_frame)
        try: drv.switch_to.default_content()
        except Exception: pass
        # 이미지맵(area 태그)로 찾은 게 있으면 우선 사용(가장 정확), 없으면 JS 결과
        area = [z for z in allz if z.get('src') == 'area']
        zones = self._dedup_zones(area if area else allz)
        return self._sort_zones(zones)

    # 구역을 라벨 속 숫자 기준 오름차순 정렬 (숫자 없으면 뒤로)
    def _sort_zones(self, zones):
        def key(z):
            m = re.search(r"\d+", z.get('label', ''))
            return (0, int(m.group())) if m else (1, z.get('label', ''))
        try:
            return sorted(zones, key=key)
        except Exception:
            return zones

    # ── area(구역) 경계 박스 계산 (natural 좌표) ──
    def _area_bbox(self, shape, coords):
        try:
            nums = [float(x) for x in re.split(r"[ ,]+", (coords or '').strip()) if x != '']
        except Exception:
            return None
        if not nums:
            return None
        shape = (shape or '').lower()
        if shape == 'circle' and len(nums) >= 3:
            cx, cy, r = nums[0], nums[1], nums[2]
            return (cx - r, cy - r, cx + r, cy + r)
        if shape == 'rect' and len(nums) >= 4:
            return (min(nums[0], nums[2]), min(nums[1], nums[3]),
                    max(nums[0], nums[2]), max(nums[1], nums[3]))
        xs, ys = nums[0::2], nums[1::2]
        if xs and ys:
            return (min(xs), min(ys), max(xs), max(ys))
        return None

    # ── 좌석배치도 이미지에서 각 구역의 채움색 샘플링 ──
    # 구역 영역 안을 격자로 찍어 가장 많이 나온 유채색을 그 구역 색으로 본다.
    # 반환: areas 와 같은 길이의 [r,g,b] 또는 None 리스트
    def _sample_area_colors(self, areas):
        drv = self.driver
        colors = [None] * len(areas)
        # usemap 이미지(좌석배치도) 찾기 - 가장 큰 것
        img = None
        try:
            imgs = drv.find_elements(By.CSS_SELECTOR, "img[usemap]")
            if imgs:
                img = max(imgs, key=lambda e: (e.size.get('width', 0) *
                                               e.size.get('height', 0)) if e.size else 0)
        except Exception:
            img = None
        if img is None:
            return colors
        try:
            im = Image.open(io.BytesIO(img.screenshot_as_png)).convert("RGB")
        except Exception:
            return colors
        sw, sh = im.size
        try:
            nat = drv.execute_script(
                "var i=arguments[0];return [i.naturalWidth,i.naturalHeight];", img)
            nw = nat[0] or sw
            nh = nat[1] or sh
        except Exception:
            nw, nh = sw, sh
        sx = sw / float(nw) if nw else 1.0
        sy = sh / float(nh) if nh else 1.0
        px = im.load()

        def colored(rgb):
            r, g, b = rgb[0], rgb[1], rgb[2]
            if r > 235 and g > 235 and b > 235:           # 흰색/배경
                return False
            if max(r, g, b) - min(r, g, b) < 22 and 90 < min(r, g, b) < 215:  # 회색
                return False
            return True

        for idx, a in enumerate(areas):
            box = self._area_bbox(a.get('shape', ''), a.get('coords', ''))
            if not box:
                continue
            x0, y0, x1, y1 = (box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy)
            # 테두리/라벨 영향을 줄이려 중앙 60% 영역만 격자 샘플링
            w, h = x1 - x0, y1 - y0
            ax0, ay0 = x0 + w * 0.2, y0 + h * 0.2
            ax1, ay1 = x1 - w * 0.2, y1 - h * 0.2
            counts = {}
            steps = 6
            for ix in range(steps + 1):
                for iy in range(steps + 1):
                    x = int(ax0 + (ax1 - ax0) * ix / steps)
                    y = int(ay0 + (ay1 - ay0) * iy / steps)
                    if 0 <= x < sw and 0 <= y < sh:
                        rgb = px[x, y]
                        if colored(rgb):
                            key = (rgb[0] // 12 * 12, rgb[1] // 12 * 12, rgb[2] // 12 * 12)
                            counts[key] = counts.get(key, 0) + 1
            if counts:
                best = max(counts.items(), key=lambda kv: kv[1])[0]
                colors[idx] = [best[0], best[1], best[2]]
        return colors

    # ── 한 프레임 안에서 구역 수집 ─────────────
    # (1) area 태그 이미지맵 (2) JS 텍스트/도형 스캔
    def _scan_zones_current_frame(self):
        drv = self.driver
        zones = []
        # (1) area 태그 (BookMain.asp 좌석 이미지맵)
        # 구역 번호는 이미지에 그려져 DOM 텍스트엔 없는 경우가 많다. title/alt 가
        # 있으면 그걸, 없으면 href 의 숫자를 라벨로 쓴다. 클릭은 href 실행이 확실.
        # (등급 매칭은 색이 아니라 구역에 들어가 좌석 등급을 직접 읽어 판단)
        try:
            for a in drv.find_elements(By.TAG_NAME, "area"):
                title  = (a.get_attribute("title") or a.get_attribute("alt") or "").strip()
                href   = (a.get_attribute("href") or "").strip()
                label = title
                if not label and href:
                    m = re.findall(r"\d{1,4}", href)
                    if m:
                        label = m[-1]
                if not label:
                    continue
                zones.append({'label': label, 'href': href,
                              'color': None, 'src': 'area'})
        except Exception:
            pass
        # (2) SVG/HTML 텍스트+도형 스캔
        js = r"""
        function toRGB(s){
            if(!s) return null;
            var m=s.match(/(\d+),\s*(\d+),\s*(\d+)/);
            if(m) return [+m[1],+m[2],+m[3]];
            var h=s.replace(/^#/,'');
            if(h.length===3) h=h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
            if(h.length===6) return [parseInt(h.slice(0,2),16),
                                     parseInt(h.slice(2,4),16),
                                     parseInt(h.slice(4,6),16)];
            return null;
        }
        function colorOf(el){
            if(!el) return null;
            var cs=window.getComputedStyle(el);
            var cands=[el.getAttribute&&el.getAttribute('fill'), cs.fill,
                       cs.backgroundColor, (el.style&&el.style.fill)];
            for(var k=0;k<cands.length;k++){
                var rgb=toRGB(cands[k]||'');
                if(!rgb) continue;
                var r=rgb[0],g=rgb[1],b=rgb[2];
                if(r>245&&g>245&&b>245) continue;   // 흰색
                if(r<12&&g<12&&b<12) continue;      // 검정
                return rgb;
            }
            return null;
        }
        // 라벨 노드 기준으로 자신→형제도형→부모 순서로 색을 찾음
        function findColor(textEl){
            var node=textEl;
            for(var d=0; d<5 && node; d++){
                var c=colorOf(node);
                if(c) return c;
                var par=node.parentElement;
                if(par){
                    var shapes=par.querySelectorAll('rect,polygon,path,circle');
                    for(var i=0;i<shapes.length;i++){
                        var cc=colorOf(shapes[i]);
                        if(cc) return cc;
                    }
                }
                node=node.parentElement;
            }
            return null;
        }
        var out=[], seen={};
        var nodes=document.querySelectorAll('text,tspan,a,g,span,div,li,td');
        for(var i=0;i<nodes.length;i++){
            var t=(nodes[i].textContent||'').trim();
            if(/^[A-Z가-힣]?\d{1,3}$/.test(t)||/^[A-Z가-힣]$/.test(t)){
                if(seen[t]) continue;
                seen[t]=1;
                out.push({label:t, color:findColor(nodes[i])});
            }
        }
        return out;
        """
        try:
            for z in (drv.execute_script(js) or []):
                z['src'] = 'js'
                zones.append(z)
        except Exception:
            pass
        return zones

    def _dedup_zones(self, items):
        # href가 있으면 href로(서로 다른 area 모두 유지), 없으면 label로 중복 제거
        seen, out = set(), []
        for x in items:
            key = x.get('href') or x.get('label', '')
            if key and key not in seen:
                seen.add(key); out.append(x)
        return out

    def _dedup(self, items):
        seen, out = set(), []
        for x in items:
            if x not in seen:
                seen.add(x); out.append(x)
        return out

    # ── 펼쳐진 패널에서 'N영역' 링크 읽기 JS ───
    _JS_READ_ZONES = r"""
    var out=[];
    var nodes=document.querySelectorAll('a');
    for(var i=0;i<nodes.length;i++){
        var el=nodes[i];
        var m=(el.textContent||'').match(/(\d{1,4})\s*영역/);
        if(m){
            var b=el.getBoundingClientRect();
            if(b.width>0 && b.height>0){
                out.push({label:m[1], href:el.getAttribute('href')||'',
                          onclick:el.getAttribute('onclick')||''});
            }
        }
    }
    return out;
    """

    # ── 요소 안전 클릭 (네이티브 우선, JS 폴백) ──
    def _safe_click(self, el):
        drv = self.driver
        try:
            drv.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        except Exception:
            pass
        try:
            el.click()
            return True
        except Exception:
            try:
                drv.execute_script("arguments[0].click();", el)
                return True
            except Exception:
                return False

    # ── 선택 등급의 구역 읽기 (좌석등급 패널) ──
    # 좌석등급 패널에서 선택 등급명을 가진 요소를 '네이티브 클릭'해 펼친 뒤,
    # 그때 나타나는 'N영역' 링크들을 읽는다. (모든 프레임 재귀)
    def _get_zones_for_grade(self, gname, depth=0):
        drv = self.driver
        g = (gname or "").replace(" ", "")
        # 1) 현재 프레임: 등급명을 가진 짧은 텍스트 요소들을 후보로 모아 클릭 시도
        cands = []
        try:
            xp = "//*[contains(normalize-space(.), '%s')]" % gname
            for el in drv.find_elements(By.XPATH, xp):
                try:
                    t = (el.text or "").replace(" ", "")
                    if t and len(t) <= 40 and g in t:
                        cands.append((len(t), el))
                except Exception:
                    continue
        except Exception:
            pass
        cands.sort(key=lambda x: x[0])   # 짧은 텍스트(등급명 자체) 우선
        for _, el in cands[:6]:
            try:
                self._safe_click(el)
                self._wait(0.7)
                for _r in range(2):
                    try:
                        zs = drv.execute_script(self._JS_READ_ZONES) or []
                    except Exception:
                        zs = []
                    if zs:
                        return zs
                    self._wait(0.4)
            except Exception:
                continue
        if depth >= 4:
            return []
        # 2) 하위 프레임 재귀
        try:
            cnt = len(drv.find_elements(By.TAG_NAME, "iframe"))
        except Exception:
            cnt = 0
        for i in range(cnt):
            try:
                frames = drv.find_elements(By.TAG_NAME, "iframe")
                if i >= len(frames):
                    break
                drv.switch_to.frame(frames[i])
                zs = self._get_zones_for_grade(gname, depth + 1)
                if zs:
                    return zs
                drv.switch_to.parent_frame()
            except Exception:
                try: drv.switch_to.parent_frame()
                except Exception:
                    try: drv.switch_to.default_content()
                    except Exception: pass
        return []

    # ── 등급 패널 구조 진단 (구역 못 읽을 때) ──
    def _diagnose_grade_panel(self, gname):
        g = (gname or "").replace(" ", "")
        js = r"""
        var g=arguments[0];
        function norm(s){return (s||'').replace(/\s+/g,'');}
        var gradeHits=[], areaCnt=0;
        var nodes=document.querySelectorAll('a,li,dd,dt,span,div,td,p,strong,b');
        for(var i=0;i<nodes.length;i++){
            var raw=(nodes[i].textContent||'');
            if(/(\d{1,4})\s*영역/.test(raw)) areaCnt++;
            var t=norm(raw);
            if(t.length>0 && t.length<=24 && g && t.indexOf(g)>=0 && gradeHits.length<4){
                gradeHits.push(nodes[i].tagName+':'+t.slice(0,24));
            }
        }
        return {grade:gradeHits, area:areaCnt};
        """
        def run():
            try:
                r = self.driver.execute_script(js, g)
                return [r] if r and (r.get('grade') or r.get('area')) else []
            except Exception:
                return []
        drv = self.driver
        try: drv.switch_to.default_content()
        except Exception: pass
        rows = self._collect_all_frames(run)
        try: drv.switch_to.default_content()
        except Exception: pass
        if rows:
            self.log("[진단] 등급/구역 패널:")
            for r in rows[:4]:
                self.log(f"  등급매칭 {r.get('grade')}, 영역링크수={r.get('area')}")
        else:
            self.log(f"[진단] '{gname}' 패널 요소를 못 찾음")

    # ── 신형 onestop 좌석 페이지 (tickets.interpark.com/onestop/seat) ──
    def _is_onestop(self):
        return "/onestop" in self._url()

    # 좌석배치도(가장 큰 SVG/캔버스 영역) 안의 구역 라벨(314, A 등)을 찾고,
    # 라벨 위치 아래에 깔린 도형의 채움색을 구역 색으로 읽는다.
    # 찾은 라벨 요소에는 data-macro-zone 속성을 붙여 나중에 클릭할 때 쓴다.
    _JS_ONESTOP_ZONES = r"""
    function toRGB(s){
        if(!s||s==='none'||s==='transparent') return null;
        var m=s.match(/(\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?/);
        if(m){ if(m[4]!==undefined && +m[4]===0) return null;
               return [+m[1],+m[2],+m[3]]; }
        var h=s.replace(/^#/,'');
        if(h.length===3) h=h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
        if(/^[0-9a-f]{6}$/i.test(h)) return [parseInt(h.slice(0,2),16),
            parseInt(h.slice(2,4),16), parseInt(h.slice(4,6),16)];
        return null;
    }
    function fillOf(el){
        try{
            var cs=getComputedStyle(el);
            var c=[el.getAttribute&&el.getAttribute('fill'), cs.fill, cs.backgroundColor];
            for(var k=0;k<c.length;k++){
                var rgb=toRGB(c[k]||'');
                if(!rgb) continue;
                var r=rgb[0],g=rgb[1],b=rgb[2];
                if(r>240&&g>240&&b>240) continue;          // 흰색/배경
                return rgb;
            }
        }catch(e){}
        return null;
    }
    function vis(el){
        var b=el.getBoundingClientRect();
        return b.width>0&&b.height>0;
    }
    // 1) 좌석배치도 컨테이너: 화면에서 가장 큰 svg (없으면 canvas)
    var best=null, area=0;
    var cands=document.querySelectorAll('svg,canvas');
    for(var i=0;i<cands.length;i++){
        var b=cands[i].getBoundingClientRect();
        var a=b.width*b.height;
        if(a>area){ area=a; best=cands[i]; }
    }
    var box = best ? best.getBoundingClientRect() : null;
    // 아이콘 같은 작은 svg 뿐이면 배치도가 아님 → 영역 필터 생략
    if(area < 40000) box = null;
    var LABEL=/^(?:[A-Z]|[A-Z]?[1-9]\d{0,3}|[가-힣]{1,2}\d{0,3})$/;
    var SKIP={'STAGE':1,'FOH':1};
    var out=[], seen={};
    var nodes=document.querySelectorAll('text,tspan,span,div,p,strong,b,button');
    for(var i=0;i<nodes.length;i++){
        var el=nodes[i];
        if(el.children && el.children.length>0 &&
           (el.tagName||'').toLowerCase()!=='text') continue;  // 잎 노드만
        var t=(el.textContent||'').replace(/\s+/g,'');
        if(!t || SKIP[t] || !LABEL.test(t) || seen[t]) continue;
        if(!vis(el)) continue;
        var r=el.getBoundingClientRect();
        var cx=r.left+r.width/2, cy=r.top+r.height/2;
        // 좌석배치도 영역 밖(헤더 날짜/시간 등)은 제외
        if(box && (cx<box.left||cx>box.right||cy<box.top||cy>box.bottom)) continue;
        // 라벨 아래 깔린 도형의 색
        var color=null, shape=null;
        var stack=document.elementsFromPoint(cx,cy)||[];
        for(var k=0;k<stack.length;k++){
            var s=stack[k];
            if(s===el || el.contains(s)) continue;
            var tg=(s.tagName||'').toLowerCase();
            if(tg==='text'||tg==='tspan'||tg==='svg'||tg==='html'||tg==='body') continue;
            var c=fillOf(s);
            if(c){ color=c; shape=s; break; }
        }
        seen[t]=1;
        el.setAttribute('data-macro-zone', t);
        if(shape) shape.setAttribute('data-macro-zone-shape', t);
        out.push({label:t, color:color, x:cx, y:cy});
    }
    return {zones:out, map:best?(best.tagName+' '+Math.round(Math.sqrt(area))+'^2'):null,
            svgs:document.querySelectorAll('svg').length,
            canvas:document.querySelectorAll('canvas').length};
    """

    # 구역 번호가 글자가 아니라 그림(path)으로 그려진 배치도용:
    # 배치도 SVG 안의 '색이 칠해진 도형'을 구역으로 보고 색을 읽는다.
    # 도형에는 data-macro-zone-shape 속성을 붙여 나중에 클릭할 때 쓴다.
    _JS_ONESTOP_SHAPES = r"""
    function toRGB(s){
        if(!s||s==='none'||s==='transparent') return null;
        var m=s.match(/(\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?/);
        if(m){ if(m[4]!==undefined && +m[4]===0) return null;
               return [+m[1],+m[2],+m[3]]; }
        var h=s.replace(/^#/,'');
        if(h.length===3) h=h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
        if(/^[0-9a-f]{6}$/i.test(h)) return [parseInt(h.slice(0,2),16),
            parseInt(h.slice(2,4),16), parseInt(h.slice(4,6),16)];
        return null;
    }
    function fillOf(el){
        try{
            var cs=getComputedStyle(el);
            if(cs.display==='none'||cs.visibility==='hidden') return null;
            if(parseFloat(cs.fillOpacity)===0||parseFloat(cs.opacity)===0) return null;
            var rgb=toRGB(cs.fill||'')||toRGB(el.getAttribute('fill')||'');
            if(!rgb) return null;
            var r=rgb[0],g=rgb[1],b=rgb[2];
            if(Math.max(r,g,b)-Math.min(r,g,b)<25) return null;   // 흰/회/검정 (무대, 글자 등)
            return rgb;
        }catch(e){ return null; }
    }
    // 도형 안쪽 점(뷰포트 좌표) 찾기 - 도형의 실제 모양 기준(다른 요소에 가려져도 무관)
    function innerPoint(el, r){
        var pts=[[.5,.5],[.5,.3],[.5,.7],[.3,.5],[.7,.5],[.3,.3],[.7,.7],[.3,.7],[.7,.3],
                 [.5,.15],[.5,.85],[.15,.5],[.85,.5],[.2,.2],[.8,.8],[.2,.8],[.8,.2]];
        var svg=el.ownerSVGElement, inv=null;
        try{ inv=el.getScreenCTM().inverse(); }catch(e){}
        for(var i=0;i<pts.length;i++){
            var x=r.left+r.width*pts[i][0], y=r.top+r.height*pts[i][1];
            if(!svg||!inv||!el.isPointInFill) return [x,y];
            try{
                var p=svg.createSVGPoint(); p.x=x; p.y=y;
                if(el.isPointInFill(p.matrixTransform(inv))) return [x,y];
            }catch(e){ return [x,y]; }
        }
        return [r.left+r.width/2, r.top+r.height/2];
    }
    // 도형/상위 그룹의 속성에서 구역 이름 추출
    function labelOf(el, root){
        var n=el;
        for(var d=0; d<4 && n && n!==root; d++){
            var cand=[];
            if(n.getAttribute){
                cand.push(n.getAttribute('aria-label'));
                var tt=n.querySelector&&n.querySelector(':scope > title');
                if(tt) cand.push(tt.textContent);
                for(var k=0;k<n.attributes.length;k++){
                    var a=n.attributes[k];
                    if(/^data-/.test(a.name) && !/^data-macro/.test(a.name)
                       && /(block|zone|area|section|sector|name|label|title)/i.test(a.name))
                        cand.push(a.value);
                }
            }
            var idm=((n.getAttribute&&n.getAttribute('id'))||'').match(
                /(?:block|zone|area|section|sector)[-_ ]?([A-Za-z0-9가-힣]{1,6})$/i);
            if(idm) cand.push(idm[1]);
            for(var c=0;c<cand.length;c++){
                var v=(cand[c]||'').replace(/\s+/g,' ').trim();
                v=v.replace(/\s*(구역|영역|블록|block)$/i,'');
                if(v && v.length<=10) return v;
            }
            n=n.parentElement;
        }
        return '';
    }
    var best=null, area=0, svgs=document.querySelectorAll('svg');
    for(var i=0;i<svgs.length;i++){
        var b=svgs[i].getBoundingClientRect();
        if(b.width*b.height>area){ area=b.width*b.height; best=svgs[i]; }
    }
    if(!best || area<40000) return {zones:[], sample:''};
    var box=best.getBoundingClientRect();
    // 이전 표시 제거
    var old=best.querySelectorAll('[data-macro-zone-shape]');
    for(var i=0;i<old.length;i++) old[i].removeAttribute('data-macro-zone-shape');
    var out=[], sample='';
    var shapes=best.querySelectorAll('path,polygon,rect,circle,ellipse');
    for(var i=0;i<shapes.length;i++){
        var el=shapes[i];
        var r=el.getBoundingClientRect();
        if(r.width<12||r.height<12) continue;                 // 글자/좌석점 등
        if(r.width*r.height>area*0.6) continue;               // 배경
        var color=fillOf(el);
        if(!color) continue;
        if(!sample) sample=(el.parentElement&&el.parentElement!==best
                            ? el.parentElement.outerHTML : el.outerHTML).slice(0,400);
        var cx=(r.left+r.width/2-box.left)/box.width, cy=(r.top+r.height/2-box.top)/box.height;
        out.push({el:el, color:color, name:labelOf(el,best), cx:cx, cy:cy});
    }
    // 위→아래, 왼→오 순으로 번호
    out.sort(function(a,b){ return (Math.round(a.cy*12)-Math.round(b.cy*12)) || (a.cx-b.cx); });
    var H=['왼쪽','가운데','오른쪽'], V=['위','중간','아래'], res=[], used={};
    for(var i=0;i<out.length;i++){
        var z=out[i];
        var label=z.name;
        if(!label || used[label]) label='Z'+(i+1);
        used[label]=1;
        z.el.setAttribute('data-macro-zone-shape', label);
        res.push({label:label, color:z.color, named:!!z.name,
                  hint:H[Math.min(2,Math.floor(z.cx*3))]+' '+V[Math.min(2,Math.floor(z.cy*3))]});
    }
    return {zones:res, sample:sample};
    """

    # 표시해 둔 구역 도형의 '안쪽 점' 뷰포트 좌표 (없으면 None)
    _JS_ONESTOP_POINT = r"""
    var el=document.querySelector('[data-macro-zone-shape="'+arguments[0]+'"]');
    if(!el) el=document.querySelector('[data-macro-zone="'+arguments[0]+'"]');
    if(!el) return null;
    var r=el.getBoundingClientRect();
    var pts=[[.5,.5],[.5,.3],[.5,.7],[.3,.5],[.7,.5],[.3,.3],[.7,.7],[.3,.7],[.7,.3],
             [.5,.15],[.5,.85],[.15,.5],[.85,.5],[.2,.2],[.8,.8],[.2,.8],[.8,.2]];
    var svg=el.ownerSVGElement, inv=null;
    try{ inv=el.getScreenCTM().inverse(); }catch(e){}
    if(svg && inv && el.isPointInFill){
        for(var i=0;i<pts.length;i++){
            var x=r.left+r.width*pts[i][0], y=r.top+r.height*pts[i][1];
            try{
                var p=svg.createSVGPoint(); p.x=x; p.y=y;
                if(el.isPointInFill(p.matrixTransform(inv))) return [x,y];
            }catch(e){ break; }
        }
    }
    return [r.left+r.width/2, r.top+r.height/2];
    """

    # 좌석배치도 요소(가장 큰 svg/canvas/img) 찾기
    _JS_ONESTOP_MAP = r"""
    var best=null, area=0;
    var c=document.querySelectorAll('svg,canvas,img');
    for(var i=0;i<c.length;i++){
        var b=c[i].getBoundingClientRect();
        if(b.width*b.height>area){ area=b.width*b.height; best=c[i]; }
    }
    if(!best || area<40000) return null;
    // 실제로 화면에 보이는 부분만 (뷰포트와, 넘침을 잘라내는 상위 요소로 자른 영역)
    var r=best.getBoundingClientRect();
    var L=Math.max(r.left,0), T=Math.max(r.top,0);
    var R=Math.min(r.right,window.innerWidth), B=Math.min(r.bottom,window.innerHeight);
    for(var p=best.parentElement; p && p!==document.documentElement; p=p.parentElement){
        var cs=getComputedStyle(p);
        if((cs.overflowX+cs.overflowY).replace(/visible/g,'')==='') continue;
        var q=p.getBoundingClientRect();
        L=Math.max(L,q.left); T=Math.max(T,q.top); R=Math.min(R,q.right); B=Math.min(B,q.bottom);
    }
    if(R-L<50 || B-T<50) return null;
    return {el:best, left:L, top:T, width:R-L, height:B-T, vw:window.innerWidth,
            html:(arguments[0] ? (best.outerHTML||'').slice(0,300000) : '')};
    """

    # 배치도의 '화면에 보이는 부분'을 캡처해 (이미지, 화면상 위치정보) 반환.
    # 이미지 안의 상대 위치(fx,fy)는 m 의 left/top/width/height 로 화면 좌표가 된다.
    def _map_capture(self, with_html=False):
        drv = self.driver
        try: drv.switch_to.default_content()
        except Exception: pass
        try:
            m = drv.execute_script(self._JS_ONESTOP_MAP, bool(with_html))
            if not m:
                return None, None
            full = Image.open(io.BytesIO(drv.get_screenshot_as_png())).convert("RGB")
            k = full.width / float(m.get('vw') or full.width)      # 화면 배율
            box = (int(round(m['left'] * k)), int(round(m['top'] * k)),
                   int(round((m['left'] + m['width']) * k)),
                   int(round((m['top'] + m['height']) * k)))
            return full.crop(box), m
        except Exception:
            return None, None

    # 배치도를 화면에 보이는 그림 그대로 캡처해 색 덩어리(구역)로 나눈다.
    # svg 안의 <image>, <img> 파일, canvas 로 그려진 배치도 모두 대응.
    def _scan_onestop_image(self):
        im, m = self._map_capture(with_html=True)
        if im is None:
            return []
        self._onestop_map_html = m.get('html') or ''
        # 너무 크면 줄여서 처리 (경계선이 사라지지 않을 정도로만)
        if im.width > 1100:
            k = 1100.0 / im.width
            im = im.resize((1100, max(1, int(im.height * k))), Image.NEAREST)
        try:
            # 등급 색에 기대지 않고 배치도에 실제로 칠해진 색 그대로 구역을 나눈다
            segs = segment_zone_image(im, None)
        except Exception:
            segs = []
        H3 = ['왼쪽', '가운데', '오른쪽']; V3 = ['위', '중간', '아래']
        zones = []
        names = [z.get('name') or '' for z in segs]
        for i, z in enumerate(segs, 1):
            name = z.get('name') or ''
            if name and names.count(name) > 1:
                name = ''                      # 같은 이름이 둘이면 잘못 읽은 것
            zones.append({
                'label': name or 'Z%d' % i, 'color': z['color'], 'named': bool(name),
                'hint': H3[min(2, int(z['cx'] * 3))] + ' ' + V3[min(2, int(z['cy'] * 3))],
                'fx': z['fx'], 'fy': z['fy'], 'src': 'onestop', 'href': ''})
        return zones

    # 화면 좌표를 실제 마우스로 클릭
    def _click_viewport_point(self, x, y):
        drv = self.driver
        x, y = int(round(x)), int(round(y))
        try:
            ac = ActionChains(drv)
            ac.w3c_actions.pointer_action.move_to_location(x, y)
            ac.w3c_actions.pointer_action.click()
            ac.perform()
            return True
        except Exception:
            pass
        try:
            drv.execute_script(
                "var x=arguments[0],y=arguments[1];var e=document.elementFromPoint(x,y);"
                "if(!e)return;['pointerdown','mousedown','pointerup','mouseup','click']"
                ".forEach(function(t){e.dispatchEvent(new MouseEvent(t,{bubbles:true,"
                "cancelable:true,view:window,clientX:x,clientY:y}));});", x, y)
            return True
        except Exception:
            return False

    def _scan_onestop_shapes(self):
        drv = self.driver
        try: drv.switch_to.default_content()
        except Exception: pass
        try:
            r = drv.execute_script(self._JS_ONESTOP_SHAPES) or {}
        except Exception:
            r = {}
        zones = r.get('zones') or []
        for z in zones:
            z['src'] = 'onestop'
            z['href'] = ''
        self._onestop_sample = r.get('sample') or ''
        return zones

    def _scan_onestop_zones(self):
        drv = self.driver
        try: drv.switch_to.default_content()
        except Exception: pass
        res = []
        info = None
        def fn():
            nonlocal info
            try:
                r = drv.execute_script(self._JS_ONESTOP_ZONES) or {}
            except Exception:
                return []
            zs = r.get('zones') or []
            if zs or info is None:
                info = r
            return zs
        res = self._collect_all_frames(fn)
        try: drv.switch_to.default_content()
        except Exception: pass
        for z in res:
            z['src'] = 'onestop'
            z['href'] = ''
        if not res:
            # 구역 번호가 글자로 없으면 배치도의 색칠된 도형을 구역으로 읽는다
            shapes = self._scan_onestop_shapes()
            if shapes and all(z.get('named') for z in shapes):
                return shapes
            # 배치도를 그림으로 캡처해 색 덩어리로 구역을 나누고 이름(글자)을 읽는다
            imgz = self._scan_onestop_image()
            if imgz:
                return imgz
            if shapes:
                return shapes          # 화면 위치 순서 유지
        if not res and not getattr(self, '_dumped', False):
            self._dumped = True        # 진단 저장은 한 번만
            self._dump_page("onestop_dump.html")
            if info:
                self.log(f"[진단] 배치도={info.get('map')}, svg={info.get('svgs')}, "
                         f"canvas={info.get('canvas')}")
        return self._sort_zones(self._dedup_zones(res))

    # 구역 색과 등급 범례 색을 비교해 '가장 가까운 등급'이 선택 등급인 구역만 남긴다.
    @staticmethod
    def _cdist(a, b):
        return sum(abs(a[i] - b[i]) for i in range(3))

    @staticmethod
    def _color_ok(c):   # 유채색만 등급 색으로 인정 (회색 구분선 등을 잘못 읽은 값 제외)
        return bool(c) and max(c) - min(c) >= 40

    # 배치도에 칠해진 색 종류 (비슷한 색끼리 묶음): [[대표색, [구역...]], ...]
    def _zone_color_groups(self, zones):
        groups = []
        for z in zones:
            c = z.get('color')
            if not c:
                continue
            for gr in groups:
                if self._cdist(gr[0], c) <= 45:
                    gr[1].append(z); break
            else:
                groups.append([c, [z]])
        return groups

    # 등급 색 중 배치도의 어떤 색과도 맞지 않는(또는 못 읽은) 등급 목록
    def _unmatched_grades(self, zones, grade_list):
        groups = self._zone_color_groups(zones)
        bad = []
        for g in grade_list or []:
            c = g.get('color')
            if not (self._color_ok(c) and
                    any(self._cdist(gr[0], c) <= 60 for gr in groups)):
                bad.append(g)
        return bad

    # 모든 등급의 색이 배치도 색과 맞도록 보완한다.
    #  1) 안 맞는 등급이 있으면 '등급 별 가격' 패널을 화면 캡처해 색 점을 직접 읽음
    #  2) 그래도 하나만 남으면, 배치도에서 주인이 없는 색 하나를 그 등급 색으로 확정
    def _complete_grade_colors(self, zones, grade_list):
        if not zones or not grade_list:
            return
        bad = self._unmatched_grades(zones, grade_list)
        if bad:
            self._read_grade_colors_from_screen(grade_list, only=bad)
            self._close_price_panel()
        bad = self._unmatched_grades(zones, grade_list)
        if len(bad) == 1:
            good = [g['color'] for g in grade_list if g is not bad[0]]
            free = [gr for gr in self._zone_color_groups(zones)
                    if not any(self._cdist(gr[0], c) <= 60 for c in good)]
            if len(free) == 1:
                bad[0]['color'] = free[0][0]

    # 구역 색이 선택 등급의 색과 같은 구역만 남긴다.
    def _filter_zones_by_grade(self, zones, grade, grade_list):
        tgt = (grade or {}).get('color')
        if not self._color_ok(tgt):
            return None
        palette = [g.get('color') for g in (grade_list or [])
                   if self._color_ok(g.get('color'))]
        if tgt not in palette:
            palette.append(tgt)
        out = []
        for z in zones:
            c = z.get('color')
            if not c:
                continue
            near = min(palette, key=lambda p: self._cdist(p, c))
            if near == tgt and self._cdist(c, tgt) <= 60:
                out.append(z)
        return out

    # 등급 색을 DOM에서 못 읽었을 때: '등급 별 가격' 패널을 열고 화면을 캡처해
    # 각 등급 이름 왼쪽의 색 점을 픽셀로 읽는다. 읽은 색은 grade_list 에 채운다.
    def _read_grade_colors_from_screen(self, grade_list, only=None):
        drv = self.driver
        js = r"""
        var names=arguments[0], out=[];
        for(var k=0;k<names.length;k++){
            var best=null, bl=1e9;
            var all=document.querySelectorAll('li,tr,div,p,span,dt,dd,td');
            for(var i=0;i<all.length;i++){
                var t=(all[i].textContent||'').replace(/\s+/g,' ').trim();
                if(t.indexOf(names[k])!==0 || t.length>40) continue;
                var r=all[i].getBoundingClientRect();
                if(r.width<=0||r.height<=0) continue;
                if(t.length<bl){ bl=t.length; best=r; }
            }
            out.push(best?[best.left,best.top,best.right,best.bottom]:null);
        }
        return {rects:out, w:window.innerWidth};
        """
        names = [g.get('name', '') for g in grade_list]

        def rects():
            try:
                return drv.execute_script(js, names) or {}
            except Exception:
                return {}
        r = rects()
        if not any(r.get('rects') or []):
            self._open_price_panel()
            self._wait(0.6)
            try: drv.switch_to.default_content()
            except Exception: pass
            r = rects()
        if not any(r.get('rects') or []):
            return False
        try:
            im = Image.open(io.BytesIO(drv.get_screenshot_as_png())).convert("RGB")
        except Exception:
            return False
        k = im.width / float(r.get('w') or im.width)
        arr = np.asarray(im).astype(np.int32)
        found = False
        for g, rc in zip(grade_list, r['rects']):
            if not rc or (only and g not in only):
                continue
            # 등급 이름 바로 왼쪽(같은 줄 ~ 살짝 아래)의 색 점
            x0 = int(max(0, (rc[0] - 48) * k)); x1 = int(min(im.width, (rc[0] + 24) * k))
            y0 = int(max(0, (rc[1] - 4) * k));  y1 = int(min(im.height, (rc[3] + 12) * k))
            box = arr[y0:y1, x0:x1].reshape(-1, 3)
            if not len(box):
                continue
            satp = box[(box.max(axis=1) - box.min(axis=1)) >= 60]
            if len(satp) < 8:
                continue
            # 가장 많이 나온 색(점의 속 색)을 고른다
            keys, counts = np.unique(satp // 8, axis=0, return_counts=True)
            top = keys[counts.argmax()]
            sel = satp[(satp // 8 == top).all(axis=1)]
            g['color'] = [int(v) for v in np.median(sel, axis=0)]
            found = True
        return found

    # 구역 라벨을 실제 마우스로 클릭 (React 이벤트가 확실히 발생하도록)
    def _click_onestop_zone(self, label, zone=None):
        drv = self.driver
        # 1) 그림으로 읽은 구역: 배치도 안 상대 위치로 클릭
        if isinstance(zone, dict) and zone.get('fx') is not None:
            try:
                m = drv.execute_script(self._JS_ONESTOP_MAP)
            except Exception:
                m = None
            if not m:
                return False
            return self._click_viewport_point(m['left'] + m['width'] * zone['fx'],
                                              m['top'] + m['height'] * zone['fy'])
        # 2) DOM 에서 읽은 구역: 표시해 둔 요소의 안쪽 점 클릭
        try:
            pt = drv.execute_script(self._JS_ONESTOP_POINT, label)
        except Exception:
            pt = None
        if not pt:
            return False
        return self._click_viewport_point(pt[0], pt[1])

    # 구역 번호를 읽을 수 없는 배치도: 목록 번호를 배치도 위에 번호표로 표시해
    # 어떤 번호가 어느 구역인지 눈으로 확인할 수 있게 한다. (클릭은 통과시킴)
    def _show_zone_badges(self, zone_list):
        drv = self.driver
        try:
            m = drv.execute_script(self._JS_ONESTOP_MAP)
        except Exception:
            m = None
        pts = []
        for i, z in zone_list:          # [(목록 번호, 구역), ...]
            try:
                if z.get('fx') is not None and m:
                    pts.append([i, m['left'] + m['width'] * z['fx'],
                                m['top'] + m['height'] * z['fy']])
                else:
                    pt = drv.execute_script(self._JS_ONESTOP_POINT, z.get('label', ''))
                    if pt:
                        pts.append([i, pt[0], pt[1]])
            except Exception:
                continue
        js = r"""
        var old=document.getElementById('macro-zone-badges'); if(old) old.remove();
        var wrap=document.createElement('div'); wrap.id='macro-zone-badges';
        wrap.style.cssText='position:fixed;left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none;';
        var pts=arguments[0];
        for(var i=0;i<pts.length;i++){
            var b=document.createElement('div');
            b.textContent=pts[i][0];
            b.style.cssText='position:fixed;transform:translate(-50%,-50%);pointer-events:none;'+
              'background:#111;color:#ff0;font:bold 13px/1 Arial,sans-serif;padding:3px 5px;'+
              'border-radius:9px;border:1px solid #ff0;left:'+pts[i][1]+'px;top:'+pts[i][2]+'px;';
            wrap.appendChild(b);
        }
        document.body.appendChild(wrap);
        return pts.length;
        """
        try:
            return bool(drv.execute_script(js, pts))
        except Exception:
            return False

    def _hide_zone_badges(self):
        try:
            self.driver.execute_script(
                "var o=document.getElementById('macro-zone-badges'); if(o) o.remove();")
        except Exception:
            pass

    # 배치도 요소의 HTML만 따로 저장 (구역 이름을 읽는 방법 파악용, 개인정보 없음)
    def _save_map_html(self):
        html = getattr(self, '_onestop_map_html', '')
        if not html:
            try:
                m = self.driver.execute_script(self._JS_ONESTOP_MAP, True) or {}
                html = m.get('html') or ''
            except Exception:
                html = ''
        if not html:
            return
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])),
                                "onestop_map.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            pass

    # 구조 파악용으로 현재 페이지 HTML을 파일로 저장
    def _dump_page(self, name):
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            self.log(f"[진단] 페이지 구조를 저장했습니다: {path}")
        except Exception:
            pass

    # ── 신형 onestop: 배치도 캡처 / 전체보기 복귀 / 좌석 클릭 ──
    def _onestop_map_shot(self):
        return self._map_capture()

    # 지금 배치도가 '전체 구역 보기' 상태인지 (색칠된 큰 구역이 보이면 전체 보기)
    def _onestop_is_overview(self):
        im, _ = self._onestop_map_shot()
        if im is None:
            return None
        if im.width > 1100:
            k = 1100.0 / im.width
            im = im.resize((1100, max(1, int(im.height * k))), Image.NEAREST)
        try:
            return len(segment_zone_image(im, None, read_names=False)) > 0
        except Exception:
            return None

    # 배치도 영역 안의 '글자 없는 아이콘 버튼'들 (아래쪽 것부터): +, -, 전체보기
    _JS_ONESTOP_ICON_BUTTONS = r"""
    var m=arguments[0], out=[];
    var els=document.querySelectorAll('button,[role="button"]');
    for(var i=0;i<els.length;i++){
        var e=els[i], r=e.getBoundingClientRect();
        if(r.width<20||r.height<20||r.width>120||r.height>120) continue;
        if((e.textContent||'').replace(/\s+/g,'').length>1) continue;   // 아이콘 버튼만
        var cx=r.left+r.width/2, cy=r.top+r.height/2;
        if(cx<m.left||cx>m.left+m.width||cy<m.top||cy>m.top+m.height) continue;
        out.push([cx,cy,r.bottom]);
    }
    out.sort(function(a,b){return b[2]-a[2];});
    return out;
    """

    # 좌석 화면(확대 상태)에서 전체 구역 보기로 되돌린다.
    def _onestop_back_to_overview(self):
        if self._onestop_is_overview():
            return True
        drv = self.driver
        try:
            m = drv.execute_script(self._JS_ONESTOP_MAP)
            btns = drv.execute_script(self._JS_ONESTOP_ICON_BUTTONS, m) or []
        except Exception:
            btns = []
        # 1) 맨 아래 아이콘 버튼 = 전체보기(화면 맞춤)
        if btns:
            self._click_viewport_point(btns[0][0], btns[0][1])
            self._wait(0.6)
            if self._onestop_is_overview():
                return True
        # 2) 그 위 버튼 = 축소(-) 를 여러 번
        if len(btns) >= 2:
            for _ in range(8):
                self._click_viewport_point(btns[1][0], btns[1][1])
                self._wait(0.25)
                if self._onestop_is_overview():
                    return True
        if not getattr(self, '_overview_warned', False):
            self._overview_warned = True
            self.log("전체 구역 보기로 돌아가지 못했습니다 (전체보기 버튼을 못 찾음). "
                     "배치도 오른쪽 아래 [화면 맞춤] 버튼을 직접 눌러 주세요.")
        return False

    # 좌석 화면에서 선택 등급 색의 예매 가능 좌석을 찾아 클릭한다.
    def _click_onestop_seat(self, grade=None):
        if self._seat_selected() is True:
            self.log("→ 좌석 이미 선택됨")
            return True
        target = (grade or {}).get('color')
        if not self._color_ok(target):
            target = None

        def scan():
            im, m = self._map_capture()
            if im is None:
                return None, [], 0
            try:
                allseats = find_available_seats(im, None)
                seats = find_available_seats(im, target) if target else allseats
            except Exception:
                return m, [], 0
            return m, seats, len(allseats)

        # 확대 애니메이션이 끝나 화면이 멈출 때까지(두 번 연속 같은 결과) 기다린다
        m, seats, n_all = scan()
        for _ in range(6):
            self._wait(0.25)
            m2, seats2, n_all2 = scan()
            same = (len(seats2) == len(seats) and n_all2 == n_all and all(
                abs(a['fx'] - b['fx']) < 0.004 and abs(a['fy'] - b['fy']) < 0.004
                for a, b in zip(seats, seats2)))
            m, seats, n_all = m2, seats2, n_all2
            if same:
                break
        if not seats or m is None:
            if n_all and not getattr(self, '_othercolor_warned', False):
                self._othercolor_warned = True
                self.log(f"※ 선택 등급 색이 아닌 좌석만 {n_all}개 보여 건너뜁니다.")
            return False

        import random
        random.shuffle(seats)
        self.log(f"예매 가능 좌석 {len(seats)}개 발견 → 클릭 시도")
        drv = self.driver
        for st in seats[:3]:
            x = m['left'] + m['width'] * st['fx']
            y = m['top'] + m['height'] * st['fy']
            # 그 자리에 다른 요소(버튼, 패널 등)가 덮여 있으면 건너뜀
            try:
                on_map = drv.execute_script(
                    "var e=document.elementFromPoint(arguments[1],arguments[2]);"
                    "var m=arguments[0];return !!e && (e===m || m.contains(e) || e.contains(m));",
                    m['el'], x, y)
            except Exception:
                on_map = True
            if not on_map:
                continue
            self._click_viewport_point(x, y)
            # 선택 반영(서버 응답)을 잠깐 기다린다 — 성급히 다른 좌석을 또 누르지 않게
            for _ in range(8):
                self._wait(0.2)
                self._accept_alert()
                if self._seat_selected() is True:
                    self.log("→ 좌석 선택 성공")
                    return True
        self.log("좌석을 눌렀지만 선택되지 않았습니다 (다음 구역으로)")
        return False

    # ── 신형 onestop: 구역 목록 표시 + 선택 ────
    # 형식:  1. 모두 / 2. A / 3. B ... / 11. 207 ...
    def _choose_onestop_zones(self, zone_list):
        def key(z):
            lbl = z.get('label', '')
            if not z.get('named', True):
                return (2, 0, '')                       # 이름 못 읽은 구역은 맨 뒤
            if lbl.isdigit():
                return (1, int(lbl), lbl)               # 숫자 구역: 오름차순
            return (0, 0, lbl)                          # 알파벳 구역 먼저
        zone_list = sorted(zone_list, key=key)          # (안정 정렬: 같은 순위는 화면 순서)
        self.log("좌석 구역을 선택해주세요")
        self.log("1. 모두")
        unnamed = []
        for i, z in enumerate(zone_list, 2):
            if z.get('named', True):
                self.log(f"{i}. {z.get('label', '')}")
            else:
                z['disp'] = f"{i}번 구역"
                unnamed.append((i, z))
                self.log(f"{i}. (이름을 못 읽은 구역 - 배치도의 번호표 {i})")
        if unnamed and self._show_zone_badges(unnamed):
            self.log("※ 이름을 못 읽은 구역은 배치도 위에 노란 번호표로 표시했습니다.")
        self.log("번호 입력(','로 여러개). 1 또는 그냥 Enter = 모두")
        ans = self._ask(timeout=120)
        self._hide_zone_badges()
        if not ans:
            return zone_list
        self.log(f"→ {ans}")
        by_name = {z.get('label', '').upper(): z for z in zone_list if z.get('named', True)}
        selected = []
        for part in ans.split(","):
            part = part.strip()
            if not part:
                continue
            z = None
            if part.isdigit():
                n = int(part)
                if n == 1:
                    return zone_list                    # 모두
                if 2 <= n <= len(zone_list) + 1:
                    z = zone_list[n - 2]
            if z is None:
                z = by_name.get(part.upper())           # 구역 이름을 직접 입력한 경우
            if z is None:
                self.log(f"'{part}' 은(는) 목록에 없어 건너뜁니다.")
            elif z not in selected:
                selected.append(z)
        return selected if selected else zone_list

    # ── 구역 선택 (등급 클릭 후 펼쳐진 구역만 표시; 색 미사용) ──
    def _ask_zones(self, grade=None, grade_list=None):
        gname = (grade or {}).get('name', '')
        onestop = self._is_onestop()
        if onestop:
            # 등급 별 가격 패널이 배치도를 가릴 수 있으니 닫고 읽는다
            self._close_price_panel()
            self._wait(0.3)
            all_zones = self._scan_onestop_zones()
            self._save_map_html()
        else:
            all_zones = self._get_zones()
        zone_list = all_zones

        if onestop:
            if gname:
                self._complete_grade_colors(all_zones, grade_list)
                zs = self._filter_zones_by_grade(all_zones, grade, grade_list)
                if zs:
                    zone_list = zs
                    self.log(f"[{gname}] 구역 {len(zone_list)}개 (색상으로 매핑)")
                elif not (grade or {}).get('color'):
                    self.log(f"'{gname}' 등급 색을 못 읽음 → 전체 구역 표시")
                else:
                    self.log(f"'{gname}' 색과 같은 구역을 못 찾음 → 전체 구역 표시")
            sample = getattr(self, '_onestop_sample', '')
            if sample and any(z.get('hint') and not z.get('named') for z in all_zones):
                self.log("[진단] 구역 도형 예시: " + sample[:300])
        elif gname:
            # 1) 좌석등급 패널에서 선택 등급을 클릭해 펼친 뒤 구역을 읽는다
            zs = self._get_zones_for_grade(gname)
            # 2) 비면 '가격 전체보기'를 열고 재시도
            if not zs:
                self._open_price_panel(); self._wait(0.6)
                zs = self._get_zones_for_grade(gname)
            try: self.driver.switch_to.default_content()
            except Exception: pass
            if not zs:
                self._diagnose_grade_panel(gname)   # 구조 진단
            if zs:
                zlist = []
                for z in zs:
                    href = z.get('href') or ''
                    if not href and z.get('onclick'):
                        href = 'javascript:' + z['onclick']
                    zlist.append({'label': z.get('label', ''), 'href': href,
                                  'color': None, 'src': 'panel'})
                zone_list = self._dedup_zones(zlist)
                self.log(f"[{gname}] 구역 {len(zone_list)}개 (가격표에서 매핑)")
            else:
                self.log(f"'{gname}' 구역 매핑을 못 읽음 → 전체 구역 표시")
                zone_list = all_zones

        if onestop and zone_list:
            return self._choose_onestop_zones(zone_list)
        unnamed = onestop and any(z.get('hint') and not z.get('named') for z in zone_list)
        if not unnamed:
            zone_list = self._sort_zones(zone_list)
        if not zone_list:
            self.log("구역 목록을 읽지 못했습니다. 직접 입력하세요 (예: 105,106)")
        else:
            for i, z in enumerate(zone_list, 1):
                lbl = z.get('label', '')
                if z.get('hint') and not z.get('named'):
                    # 구역 번호를 읽을 수 없는 배치도: 화면상 위치로 안내
                    self.log(f"{i}. 구역 ({z['hint']})")
                elif '영역' in lbl or '구역' in lbl:
                    self.log(f"{i}. {lbl}")
                else:
                    self.log(f"{i}. {lbl}" + ("구역" if onestop else "영역"))
        if unnamed and zone_list:
            for i, z in enumerate(zone_list, 1):
                z['disp'] = f"{i}번"
            if self._show_zone_badges(list(enumerate(zone_list, 1))):
                self.log("※ 배치도 위에 노란 번호표로 목록 번호를 표시했습니다.")
        self.log("구역 번호 입력(','로 여러개). 그냥 Enter 시 전체 구역을 순회합니다.")
        ans = self._ask(timeout=120)
        self._hide_zone_badges()
        if not ans:
            return zone_list            # 전체 구역 순회
        self.log(f"→ {ans}")
        selected = []
        for part in ans.split(","):
            part = part.strip()
            if not part: continue
            try:
                idx = int(part) - 1
                if 0 <= idx < len(zone_list):
                    selected.append(zone_list[idx])
                else:
                    selected.append({'label': part, 'href': '', 'color': None})
            except:
                selected.append({'label': part, 'href': '', 'color': None})
        return selected if selected else zone_list

    # ── 좌석 클릭 ─────────────────────────────
    # grade: {'name','color':[r,g,b]} 또는 None(모두)
    def _click_seat(self, grade=None):
        if self._is_onestop():
            return self._click_onestop_seat(grade)
        drv = self.driver
        target = (grade or {}).get("color")  # [r,g,b] 또는 None

        # 이미 좌석이 선택돼 있으면(총 N석>0) 추가 클릭 없이 성공 처리
        if self._seat_selected() is True:
            self.log("→ 좌석 이미 선택됨")
            return True

        js = r"""
        var target = arguments[0];   // [r,g,b] 또는 null
        function parseColor(s){
            if(!s||s==='none'||s==='transparent') return null;
            var m=s.match(/(\d+),\s*(\d+),\s*(\d+)/);
            if(m) return {r:+m[1],g:+m[2],b:+m[3]};
            var h=s.replace(/^#/,'');
            if(h.length===3) h=h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
            if(h.length===6) return {
                r:parseInt(h.slice(0,2),16),
                g:parseInt(h.slice(2,4),16),
                b:parseInt(h.slice(4,6),16)};
            return null;
        }
        function getRGB(el){
            try {
                var cs = window.getComputedStyle(el);
                // 1) SVG fill attribute
                var r = parseColor(el.getAttribute('fill')||'');
                if(r) return r;
                // 2) CSS fill (SVG)
                r = parseColor(cs.fill||'');
                if(r) return r;
                // 3) background-color (td/div 등 일반 요소)
                r = parseColor(cs.backgroundColor||'');
                if(r) return r;
                // 4) inline style backgroundColor
                r = parseColor((el.style&&el.style.backgroundColor)||'');
                if(r) return r;
            } catch(e){}
            return null;
        }
        function isGray(r,g,b){
            return Math.max(r,g,b)-Math.min(r,g,b)<28 && Math.min(r,g,b)>110;
        }
        function matchColor(rgb){
            if(!rgb) return false;
            if(isGray(rgb.r,rgb.g,rgb.b)) return false;   // 회색=매진 제외
            if(rgb.r>245&&rgb.g>245&&rgb.b>245) return false; // 흰색 제외
            if(!target) return true;                       // 모두: 색 있는 좌석
            // 선택한 등급 색상과의 거리
            var d=Math.abs(rgb.r-target[0])+Math.abs(rgb.g-target[1])+
                  Math.abs(rgb.b-target[2]);
            return d<=70;
        }
        function clsStr(el){
            var c=(el.className&&el.className.baseVal!==undefined)
                  ?el.className.baseVal:(el.className||'');
            return (''+c).toLowerCase();
        }
        function isSoldByClass(el){
            var c=clsStr(el);
            return c.indexOf('sold')>=0||c.indexOf('disable')>=0||
                   c.indexOf('reserved')>=0||c.indexOf('unavailab')>=0;
        }
        function isLegend(el){
            // 범례 스와치(cls=lv 등) 제외 — 좌석 아님
            var c=clsStr(el);
            return c==='lv'||c.indexOf('legend')>=0||c.indexOf('grade')>=0;
        }
        var seats = document.querySelectorAll(
            'rect[fill], circle[fill], path[fill], rect[class], circle[class],' +
            'td, td[bgcolor], td[style], div[style*="background"],' +
            'span[class], img, rect, circle');
        var cands = [];
        for(var i=0;i<seats.length;i++){
            var el=seats[i];
            if(el.getAttribute && el.getAttribute('aria-disabled')==='true') continue;
            if(isSoldByClass(el) || isLegend(el)) continue;
            var bnd=el.getBoundingClientRect?el.getBoundingClientRect():null;
            if(!bnd||bnd.width<3||bnd.height<3) continue;
            if(!matchColor(getRGB(el))) continue;
            cands.push(el);
        }
        if(cands.length===0) return 0;
        // 같은 색 좌석 중 무작위 선택
        var picked=cands[Math.floor(Math.random()*cands.length)];
        function clickable(el){
            for(var d=0; d<4 && el; d++){
                if((el.tagName||'').toLowerCase()==='a'||
                   (el.tagName||'').toLowerCase()==='td'||el.onclick||
                   (el.getAttribute&&el.getAttribute('onclick'))) return el;
                el=el.parentElement;
            }
            return null;
        }
        // onclick 가진 요소 우선, 토글이므로 '딱 한 번만' 실행
        var tgt = (typeof picked.onclick==='function') ? picked
                  : (clickable(picked) || picked);
        try{
            if(typeof tgt.onclick==='function'){ tgt.onclick.call(tgt); }
            else if(tgt.click){ tgt.click(); }
            else { tgt.dispatchEvent(new MouseEvent('click',
                {bubbles:true,cancelable:true,view:window})); }
        }catch(e){}
        return cands.length;
        """
        # 등급명(title/alt) 기반 탐지용 JS - 색이 안 잡히는 이미지 좌석 대응
        gname = (grade or {}).get("name", "")
        attr_js = r"""
        var gname = (arguments[0]||'').replace(/\s+/g,'');
        function clsOf(el){var c=el.className;if(c&&c.baseVal!==undefined)c=c.baseVal;return(''+(c||'')).toLowerCase();}
        function isSold(el){
            var c=clsOf(el);
            var t=((el.getAttribute('title')||'')+(el.getAttribute('alt')||'')).toLowerCase();
            var src=((el.getAttribute('src')||'')).toLowerCase();
            return c.indexOf('sold')>=0||c.indexOf('disable')>=0||c.indexOf('reserved')>=0
                 ||c.indexOf('unavailab')>=0||t.indexOf('판매완료')>=0
                 ||src.indexOf('sold')>=0||src.indexOf('disable')>=0||src.indexOf('_n')>=0;
        }
        var nodes=document.querySelectorAll('[title],[alt]');
        var cands=[];
        for(var i=0;i<nodes.length;i++){
            var el=nodes[i];
            var t=((el.getAttribute('title')||'')+(el.getAttribute('alt')||'')).replace(/\s+/g,'');
            if(gname && t.indexOf(gname)<0) continue;     // 등급명 포함 좌석만
            var v=(el.getAttribute('value')||'');
            if(v && v!=='N') continue;                    // value 있으면 N(여석)만
            if(isSold(el)) continue;
            var b=el.getBoundingClientRect?el.getBoundingClientRect():null;
            if(!b||b.width<2||b.height<2) continue;
            cands.push(el);
        }
        if(cands.length===0) return 0;
        var p=cands[Math.floor(Math.random()*cands.length)];
        try{
            if(typeof p.onclick==='function'){ p.onclick.call(p); }
            else if(p.click){ p.click(); }
            else { p.dispatchEvent(new MouseEvent('click',
                {bubbles:true,cancelable:true,view:window})); }
        }catch(e){}
        return cands.length;
        """

        # 0) 좌석 title 의 등급명으로 매칭 (가장 정확) + onclick(SelectSeat) 직접 실행
        # 좌석: <span class="SeatN" title="[지정석 R석] ..." onclick="SelectSeat(this,...)">
        title_js = r"""
        var gname=(arguments[0]||'').replace(/\s+/g,'');
        var nodes=document.querySelectorAll("[onclick*='SelectSeat']");
        var cands=[];
        for(var i=0;i<nodes.length;i++){
            var el=nodes[i];
            var title=(el.getAttribute('title')||'').replace(/\s+/g,'');
            if(gname && title.indexOf(gname)<0) continue;   // 선택 등급 좌석만
            var b=el.getBoundingClientRect?el.getBoundingClientRect():null;
            if(!b||b.width<2||b.height<2) continue;
            cands.push(el);
        }
        if(cands.length===0) return 0;
        var p=cands[Math.floor(Math.random()*cands.length)];   // 같은 등급 중 랜덤
        // 좌석 선택은 토글이므로 '딱 한 번만' 실행 (여러번이면 도로 해제됨)
        try{
            if(typeof p.onclick==='function'){ p.onclick.call(p); }
            else if(p.click){ p.click(); }
            else { p.dispatchEvent(new MouseEvent('click',
                {bubbles:true,cancelable:true,view:window})); }
        }catch(e){}
        return cands.length;
        """

        # 1) title(등급명) 기반 탐지 (우선)
        try:
            drv.switch_to.default_content()
        except Exception:
            pass
        try:
            n = self._click_seat_recursive(title_js, gname, 0)
        except Exception:
            n = 0
        self._accept_alert()
        # 2) 못 찾으면 색 기반 탐지
        if not n:
            try: drv.switch_to.default_content()
            except Exception: pass
            try:
                n = self._click_seat_recursive(js, target, 0)
            except Exception:
                n = 0
            self._accept_alert()
        # 3) 그래도 못 찾으면 등급명 속성(title/alt) 기반
        if not n:
            try: drv.switch_to.default_content()
            except Exception: pass
            try:
                n = self._click_seat_recursive(attr_js, gname, 0)
            except Exception:
                n = 0
            self._accept_alert()
        if n and n > 0:
            disp = gname or "모두"
            self.log(f"[{disp}] 빈 좌석 클릭 시도 (후보 {n}개)")
            # 선택 반영이 늦을 수 있어 최대 ~4초간 '총 N석'을 폴링
            for _ in range(8):
                self._accept_alert()
                if self._seat_selected() is True:
                    self.log("→ 좌석 선택됨")
                    return True
                self._wait(0.5)
            self.log("→ 아직 선택 안 됨(총 0석), 계속 탐색")
            return False
        return False

    # 알림창(alert/confirm)이 뜨면 수락. (좌석 클릭 후 안내창 처리)
    def _accept_alert(self):
        try:
            alert = self.driver.switch_to.alert
            txt = ""
            try: txt = alert.text
            except Exception: pass
            alert.accept()
            if txt:
                self.log(f"[알림] {txt[:50]}")
            return True
        except Exception:
            return False

    # 선택좌석 패널의 '총 N석' 값으로 실제 선택 여부 확인.
    # True=선택됨, False=0석(미선택), None=확인불가
    def _seat_selected(self):
        if self._is_onestop():
            # 오른쪽 '선택 좌석 N' 의 N, 없으면 '선택한 좌석이 없습니다' 문구로 판단
            js = r"""
            var els=document.querySelectorAll('h1,h2,h3,h4,h5,strong,b,span,div,p');
            for(var i=0;i<els.length;i++){
                var t=(els[i].textContent||'').replace(/\s+/g,'');
                if(t.length>12) continue;
                var m=t.match(/^선택좌석(\d+)(석|매)?$/);
                if(m) return +m[1];
            }
            var body=(document.body?document.body.innerText:'').replace(/\s+/g,'');
            if(body.indexOf('선택한좌석이없습니다')>=0) return 0;
            if(body.indexOf('선택좌석')>=0) return -1;     // 문구는 있는데 개수 표시가 없음
            return null;
            """
            try:
                self.driver.switch_to.default_content()
                n = self.driver.execute_script(js)
            except Exception:
                return None
            if n is None or n == -1:
                return None
            return n > 0
        def fn():
            try:
                t = self.driver.execute_script(
                    "return document.body?document.body.innerText:''") or ""
                t = re.sub(r'\s+', '', t)   # 공백/줄바꿈/탭 모두 제거
                nums = [int(x) for x in re.findall(r'총(\d+)석선택', t)]
                if not nums:
                    nums = [int(x) for x in re.findall(r'총(\d+)석', t)]
                return nums
            except Exception:
                return []
        drv = self.driver
        try: drv.switch_to.default_content()
        except Exception: pass
        vals = self._collect_all_frames(fn)
        try: drv.switch_to.default_content()
        except Exception: pass
        if vals:
            return max(vals) > 0   # 어느 프레임이든 총 N석>0 이면 선택된 것
        return None

    # 현재 프레임이 좌석배치도(상세) 프레임인지 (메인 패널의 범례 오클릭 방지)
    def _is_seat_detail_frame(self):
        try:
            t = self.driver.execute_script(
                "return document.body?document.body.innerText:''") or ""
        except Exception:
            return False
        return ('좌석배치도' in t) or ('입장번호' in t) or bool(re.search(r'\d+\s*열', t))

    # 좌석배치도 프레임에서만 좌석 클릭 시도. (메인의 등급 범례 보라네모 오클릭 방지)
    def _click_seat_recursive(self, js, target, depth):
        drv = self.driver
        # 좌석상세 프레임일 때만 좌석 탐지/클릭 실행
        if self._is_seat_detail_frame():
            try:
                n = drv.execute_script(js, target)
            except Exception:
                n = 0
            if n and n > 0:
                return n
        if depth >= 4:
            return 0
        try:
            cnt = len(drv.find_elements(By.TAG_NAME, "iframe"))
        except Exception:
            cnt = 0
        for i in range(cnt):
            try:
                frames = drv.find_elements(By.TAG_NAME, "iframe")
                if i >= len(frames):
                    break
                drv.switch_to.frame(frames[i])
                n = self._click_seat_recursive(js, target, depth + 1)
                if n and n > 0:
                    return n
                drv.switch_to.parent_frame()
            except Exception:
                try: drv.switch_to.parent_frame()
                except Exception:
                    try: drv.switch_to.default_content()
                    except Exception: pass
        return 0

    # ── 슬라이더 퍼즐(안심예매) 처리 ──────────
    # 슬라이더 퍼즐은 봇 차단용 보안장치라 자동 풀이가 불안정/부적절하다.
    # 감지되면 사용자가 직접 슬라이더를 밀어 맞추도록 안내하고, 통과될 때까지
    # 대기했다가 자동으로 이어서 진행한다. (텍스트 보안문자와 동일한 방식)
    def _puzzle_present(self):
        def fn():
            try:
                t = self.driver.execute_script(
                    "return document.body?document.body.innerText:''") or ""
                t = re.sub(r'\s+', '', t)
                return [1] if ('슬라이더를밀어' in t or '퍼즐을맞춰' in t) else []
            except Exception:
                return []
        drv = self.driver
        try: drv.switch_to.default_content()
        except Exception: pass
        hits = self._collect_all_frames(fn)
        try: drv.switch_to.default_content()
        except Exception: pass
        return len(hits) > 0

    def _solve_puzzle(self):
        if not self._puzzle_present():
            return
        self.log("슬라이더 퍼즐 인증이 떴습니다.")
        self.log("→ 예매창에서 직접 슬라이더를 밀어 퍼즐을 맞춰주세요.")
        self.log("  통과되면 자동으로 계속 진행합니다...")
        for _ in range(300):   # 최대 5분 대기
            self._wait(1)
            if not self._puzzle_present():
                self.log("→ 퍼즐 통과 확인")
                return

    # ── 구역 클릭 ─────────────────────────────
    # zone: {'label','href',...} 또는 라벨 문자열.
    # 이미지맵은 href(자바스크립트) 실행이 가장 확실하므로 모든 프레임을
    # 재귀로 훑어 해당 area 의 href 를 실행한다. 없으면 라벨/JS로 폴백.
    def _click_zone(self, zone):
        if isinstance(zone, dict):
            label = zone.get('label', '')
            href  = zone.get('href', '') or ''
        else:
            label, href = str(zone), ''
        zone_num = label.replace("구역", "").replace("영역", "").strip()
        if "(" in zone_num:
            zone_num = zone_num.split("(")[-1].replace(")", "").strip()

        drv = self.driver
        try: drv.switch_to.default_content()
        except Exception: pass
        if self._is_onestop():
            zd = zone if isinstance(zone, dict) else None
            # 앞 구역의 좌석 화면(확대 상태)에 있으면 전체 구역 보기로 되돌린다
            if zd is not None and zd.get('fx') is not None:
                if self._onestop_back_to_overview() is False:
                    # 확대된 상태에서 구역 위치를 누르면 엉뚱한 좌석이 눌릴 수 있어 건너뜀
                    return False
            if self._click_onestop_zone(zone_num, zd):
                return True
            # 화면이 다시 그려져 표시가 사라졌으면 다시 스캔 후 재시도
            for z in self._scan_onestop_zones():
                if z.get('label') == zone_num:
                    return self._click_onestop_zone(zone_num, z)
            if zone_num not in getattr(self, '_zone_warned', set()):
                self._zone_warned = getattr(self, '_zone_warned', set()) | {zone_num}
                self.log(f"'{zone_num}' 구역을 배치도에서 찾지 못했습니다 (건너뜀)")
            return False
        if self._click_zone_recursive(zone_num, href, 0):
            return True
        try: drv.switch_to.default_content()
        except Exception: pass
        return False

    # 모든 프레임을 재귀로 훑어 구역(area) 클릭 시도. 성공 시 그 프레임에 머무름.
    def _click_zone_recursive(self, zone_num, href, depth):
        drv = self.driver
        if self._click_zone_in_frame(zone_num, href):
            return True
        if depth >= 4:
            return False
        try:
            cnt = len(drv.find_elements(By.TAG_NAME, "iframe"))
        except Exception:
            cnt = 0
        for i in range(cnt):
            try:
                frames = drv.find_elements(By.TAG_NAME, "iframe")
                if i >= len(frames):
                    break
                drv.switch_to.frame(frames[i])
                if self._click_zone_recursive(zone_num, href, depth + 1):
                    return True
                drv.switch_to.parent_frame()
            except Exception:
                try: drv.switch_to.parent_frame()
                except Exception:
                    try: drv.switch_to.default_content()
                    except Exception: pass
        return False

    # 현재 프레임 안에서만 구역 클릭 시도 (프레임 전환 없음)
    def _click_zone_in_frame(self, zone_num, href):
        drv = self.driver
        # 0) 패널 링크(a[href]) 가 보관한 href 와 일치하면 그 요소를 마우스 클릭
        if href:
            try:
                for el in drv.find_elements(By.CSS_SELECTOR, "a[href]"):
                    if (el.get_attribute("href") or "") == href:
                        drv.execute_script(
                            "var e=arguments[0];"
                            "['mouseover','mousedown','mouseup','click'].forEach("
                            "function(t){e.dispatchEvent(new MouseEvent(t,"
                            "{bubbles:true,cancelable:true,view:window}));});"
                            "if(e.click)e.click();", el)
                        return True
            except Exception:
                pass
            if href.startswith("javascript:"):
                try:
                    drv.execute_script(href[len("javascript:"):])
                    return True
                except Exception:
                    pass
        # 1) 보관해 둔 href 와 일치하는 area 실행 (가장 정확)
        if href:
            try:
                for area in drv.find_elements(By.TAG_NAME, "area"):
                    if (area.get_attribute("href") or "") == href:
                        if href.startswith("javascript:"):
                            drv.execute_script(href[len("javascript:"):])
                        else:
                            drv.execute_script("arguments[0].click();", area)
                        return True
            except Exception:
                pass
        # 2) area 의 title/alt/href 에 구역번호가 들어있으면 실행
        try:
            for area in drv.find_elements(By.TAG_NAME, "area"):
                t = (area.get_attribute("title") or
                     area.get_attribute("alt") or "").strip()
                ah = area.get_attribute("href") or ""
                if (t == zone_num or t == zone_num + "구역" or t == zone_num + "영역"
                        or (zone_num and zone_num in ah)):
                    if ah.startswith("javascript:"):
                        drv.execute_script(ah[len("javascript:"):])
                    else:
                        drv.execute_script("arguments[0].click();", area)
                    return True
        except Exception:
            pass
        # 3) SPA/일반 요소 - JS 텍스트/속성 검색
        js = r"""
        var target = arguments[0];
        function tryClick(el){
            for (var d=0; d<6 && el; d++){
                var tag = (el.tagName||'').toLowerCase();
                if (tag==='a' || tag==='button' || el.onclick ||
                    el.getAttribute('role')==='button' ||
                    (el.style && el.style.cursor==='pointer')){
                    el.click(); return true;
                }
                el = el.parentElement;
            }
            return false;
        }
        var nodes = document.querySelectorAll('text,tspan,a,g,span,div,li,button,path');
        for (var i=0;i<nodes.length;i++){
            var txt=(nodes[i].textContent||'').trim();
            if(txt===target||txt===target+'구역'||txt===target+'영역'){
                if(tryClick(nodes[i])) return true;
            }
        }
        var attrs=document.querySelectorAll('[title],[data-zone],[data-area],[aria-label]');
        for(var i=0;i<attrs.length;i++){
            var t=(attrs[i].getAttribute('title')||attrs[i].getAttribute('data-zone')||
                   attrs[i].getAttribute('data-area')||attrs[i].getAttribute('aria-label')||'');
            if(t.trim()===target||t===target+'구역'||t===target+'영역'){
                if(tryClick(attrs[i])) return true;
            }
        }
        return false;
        """
        try:
            return bool(drv.execute_script(js, zone_num))
        except:
            return False

    # ── 구역 순회 ─────────────────────────────
    def _rotate_zones(self, zones, grade=None):
        cycle = 0
        consecutive_err = 0
        self._diag_done = False
        self.log(f"빈 좌석 탐색을 시작합니다 (딜레이 {self.delay}초)")
        while True:
            for zone in zones:
                self._wait(0)
                # 브라우저 세션 생존 확인 (닫히면 매크로 전체 중단)
                try:
                    _ = self.driver.current_url
                except Exception:
                    self.log("브라우저(예매창)가 종료되어 매크로를 중단합니다.")
                    raise InterruptedError

                # 안전망: 이미 좌석이 선택돼 있으면 순회 종료(→ 좌석선택완료 단계로)
                if self._seat_selected() is True:
                    self.log("→ 좌석 선택 확인, 순회 종료")
                    return

                try:
                    # 구역 클릭 (실패해도 다음 구역으로). zone 은 dict 또는 문자열.
                    if not self._click_zone(zone):
                        self._wait(0.3)     # 클릭 실패가 이어져도 헛돌지 않게
                        continue

                    self._wait(self.delay)
                    self._solve_puzzle()

                    # 예매 가능 좌석 클릭 → 성공하면 순회 종료
                    if self._click_seat(grade):
                        return
                    consecutive_err = 0

                except InterruptedError:
                    raise
                except Exception as e:
                    consecutive_err += 1
                    self.log(f"구역 오류(건너뜀): {str(e)[:60]}")
                    if consecutive_err >= 15:
                        self.log("오류가 계속되어 순회를 중단합니다."); return
                    self._wait(0.5)
            cycle += 1
            if cycle % 5 == 0:
                self.log(f"빈 좌석 탐색 중... ({cycle}바퀴째, 아직 못 찾음)")

    # ── 좌석 DOM 진단 (구조 파악용, 1회) ───────
    # 좌석을 못 잡을 때 실제 좌석 요소 샘플을 로그로 출력한다.
    def _diagnose_seats(self):
        # 좌석상세 프레임에서 '좌석처럼 보이는' 요소의 outerHTML 을 덤프해 구조 확인.
        # 좌석은 보통 onclick 이 있거나 background-image(스프라이트)로 색을 입힌다.
        js = r"""
        var out=[];
        var body=document.body?document.body.innerText:'';
        var isDetail = body.indexOf('좌석배치도')>=0 || body.indexOf('입장번호')>=0;
        if(!isDetail) return [];
        var nodes=document.querySelectorAll('img,td,span,div,a,area');
        for(var i=0;i<nodes.length && out.length<8;i++){
            var el=nodes[i];
            var oc=el.getAttribute('onclick')||'';
            var cs=getComputedStyle(el);
            var bgimg=cs.backgroundImage||'';
            var hasBg=(bgimg && bgimg!=='none');
            // 좌석 후보: onclick 있거나 배경이미지로 칠해진 작은 요소
            if(!oc && !hasBg) continue;
            var b=el.getBoundingClientRect?el.getBoundingClientRect():{width:0,height:0};
            if(b.width<2||b.width>60||b.height<2||b.height>60) continue;
            var html=(el.outerHTML||'').replace(/\s+/g,' ').slice(0,160);
            var bgi=bgimg.replace(/.*\//,'').slice(0,30);
            out.push(html+'  |bgimg='+bgi);
        }
        return out;
        """
        def run():
            try: return self.driver.execute_script(js) or []
            except Exception: return []
        drv = self.driver
        try: drv.switch_to.default_content()
        except Exception: pass
        rows = self._collect_all_frames(run)
        try: drv.switch_to.default_content()
        except Exception: pass
        if rows:
            self.log("[진단] 좌석 요소 outerHTML 샘플:")
            for r in rows[:6]:
                self.log("  " + str(r)[:170])
        else:
            self.log("[진단] 좌석 후보(onclick/배경이미지) 요소를 못 찾음")

    # ── 좌석선택완료 / 티켓가격선택 버튼 클릭 ───
    # 버튼은 보통 메인 예매 페이지(우측 패널)에 있으므로 default content 부터
    # 모든 프레임을 재귀로 훑어 클릭한다.
    def _click_complete(self):
        kws = ['좌석선택완료', '티켓가격선택', '가격선택', '선택완료']
        self.log("좌석선택완료 버튼 찾는 중...")
        drv = self.driver
        if self._is_onestop():
            try: drv.switch_to.default_content()
            except Exception: pass
            hit = None
            try:
                # 오른쪽 아래 [선택 완료] 버튼을 실제 마우스로 클릭
                for el in drv.find_elements(By.XPATH,
                        "//button[normalize-space(.)='선택 완료' or normalize-space(.)='선택완료']"):
                    if el.is_displayed() and el.is_enabled():
                        try:
                            ActionChains(drv).move_to_element(el).click().perform()
                        except Exception:
                            drv.execute_script("arguments[0].click();", el)
                        hit = '선택 완료'
                        break
            except Exception:
                pass
            if not hit:
                hit = self._click_text_button(["선택완료"])
            self._wait(1.0)
            self._accept_alert()
            self.log(f"→ [{hit}] 클릭" if hit else "→ [선택 완료] 버튼을 찾지 못했습니다")
            self._wait(1.0)
            return
        try: drv.switch_to.default_content()
        except Exception: pass
        hit = self._click_complete_recursive(kws, 0)
        try: self.driver.switch_to.default_content()
        except Exception: pass
        self._accept_alert()      # 좌석선택완료 후 확인창 처리
        self._wait(1.5)
        self._accept_alert()
        if hit:
            self.log(f"→ [{hit}] 클릭")
        else:
            self.log("→ 좌석선택완료 버튼을 찾지 못했습니다")
        self._wait(1.0)

    def _click_complete_recursive(self, kws, depth):
        drv = self.driver
        hit = self._click_complete_in_frame(kws)
        if hit:
            return hit
        if depth >= 4:
            return None
        try:
            cnt = len(drv.find_elements(By.TAG_NAME, "iframe"))
        except Exception:
            cnt = 0
        for i in range(cnt):
            try:
                frames = drv.find_elements(By.TAG_NAME, "iframe")
                if i >= len(frames):
                    break
                drv.switch_to.frame(frames[i])
                hit = self._click_complete_recursive(kws, depth + 1)
                if hit:
                    return hit
                drv.switch_to.parent_frame()
            except Exception:
                try: drv.switch_to.parent_frame()
                except Exception:
                    try: drv.switch_to.default_content()
                    except Exception: pass
        return None

    def _click_complete_in_frame(self, kws):
        js = r"""
        var kws = arguments[0];
        function clickable(el){
            for(var d=0; d<5 && el; d++){
                var tag=(el.tagName||'').toLowerCase();
                if(tag==='a'||tag==='button'||el.onclick||
                   (el.getAttribute&&el.getAttribute('onclick'))||
                   (el.getAttribute&&el.getAttribute('role')==='button')) return el;
                el=el.parentElement;
            }
            return null;
        }
        function doClick(tgt){
            // href=javascript: → 실행, onclick → 호출, 아니면 click()
            var href=(tgt.getAttribute&&tgt.getAttribute('href'))||'';
            if(href && href.indexOf('javascript:')===0){
                try{ eval(href.substring(11)); return true; }catch(e){}
            }
            try{ if(typeof tgt.onclick==='function'){ tgt.onclick.call(tgt); return true; } }catch(e){}
            try{ tgt.click(); return true; }catch(e){}
            try{ tgt.dispatchEvent(new MouseEvent('click',
                {bubbles:true,cancelable:true,view:window})); return true; }catch(e){}
            return false;
        }
        // 가장 구체적인(텍스트가 짧은) 매칭 요소를 고른다
        var nodes = document.querySelectorAll('button,a,div,span,li,input,img,td');
        for(var k=0;k<kws.length;k++){
            var kw=kws[k].replace(/\s+/g,'');
            var best=null, bestLen=1e9;
            for(var i=0;i<nodes.length;i++){
                var el=nodes[i];
                var t=(el.textContent||'').replace(/\s+/g,'');
                var v=((el.value||'')+'').replace(/\s+/g,'');
                var a=((el.getAttribute&&(el.getAttribute('alt')||el.getAttribute('title')))||'').replace(/\s+/g,'');
                var hit=(t.indexOf(kw)>=0)||(v.indexOf(kw)>=0)||(a.indexOf(kw)>=0);
                if(!hit) continue;
                var b=el.getBoundingClientRect();
                if(b.width<=0||b.height<=0) continue;
                var L=(t||v||a).length;
                if(L<bestLen){ bestLen=L; best=el; }
            }
            if(best){
                var tgt=clickable(best)||best;
                if(doClick(tgt)) return kws[k];
            }
        }
        return null;
        """
        try:
            return self.driver.execute_script(js, kws)
        except Exception:
            return None

    # ── 다음 단계(가격/할인·배송/주문자·결제) 진입 감지 ──
    # poticket 은 단계가 iframe 으로 바뀌므로 URL이 아닌 '내용'으로 판단한다.
    # 아직 좌석배치도(좌석선택)면 False, 다음 단계 키워드가 보이면 True.
    def _is_payment_page(self):
        if self._is_onestop():
            # 좌석 단계(/onestop/seat)를 벗어나 다음 단계 주소로 넘어갔는지
            u = self._url().split('?')[0].rstrip('/')
            if not u.endswith('/seat'):
                return True
        def fn():
            try:
                t = self.driver.execute_script(
                    "return document.body?document.body.innerText:''") or ""
                return [re.sub(r'\s+', '', t)]
            except Exception:
                return []
        drv = self.driver
        try: drv.switch_to.default_content()
        except Exception: pass
        texts = self._collect_all_frames(fn)
        try: drv.switch_to.default_content()
        except Exception: pass
        joined = " ".join(texts)
        # 아직 좌석 선택 화면이면 다음 단계 아님
        if "좌석배치도" in joined or "좌석을선택" in joined:
            return False
        next_kw = ["할인선택", "가격선택", "주문자확인", "배송선택", "배송지",
                   "결제수단", "최종결제", "결제하기", "예매자정보", "무통장"]
        return any(k in joined for k in next_kw)

    # ── 결제 대기 ─────────────────────────────
    def _wait_payment(self):
        self.log("취소표를 잡았습니다! 결제 페이지 대기 중 - 소리 알림 시작")

        def _beep_loop(stop_ev):
            while not stop_ev.is_set():
                try:
                    winsound.Beep(1000, 400)
                    time.sleep(0.15)
                    winsound.Beep(1300, 400)
                    time.sleep(0.5)
                except:
                    break

        stop_ev = threading.Event()
        threading.Thread(target=_beep_loop, args=(stop_ev,), daemon=True).start()

        try:
            # 최대 30분 대기
            for _ in range(1800):
                self._wait(1)
                try:
                    url = self.driver.current_url
                    src = self.driver.page_source
                    done_kw = ["BookEnd", "결제완료", "예매완료", "주문완료",
                               "step3", "payment/complete"]
                    if any(k in url for k in done_kw):
                        self.log("✅ 결제 완료!")
                        stop_ev.set(); return
                except:
                    pass
            self.log("결제 대기 시간 초과 (30분)")
        finally:
            stop_ev.set()

    # ── 메인 흐름 ─────────────────────────────
    def run(self):
        try:
            # ① 로그인 대기
            self.log("매크로 실행 시작")
            self.log("로그인을 완료해 주세요.")
            for _ in range(300):
                self._wait(1)
                if self._is_logged_in(): break
            else:
                self.log("로그인 대기 시간 초과"); return
            self.log("→ 로그인 완료")
            # 로그인 후 자동 리다이렉트가 끝나길 잠깐 대기 (조기 이동 방지)
            self._wait(2.0)

            # 로그인 직후 야놀자 도메인에 있으면 놀 인터파크 메인으로 "한 번만" 이동.
            # 단, 이미 인터파크 도메인(메인/예매창 등)에 있으면 끌고 가지 않는다.
            if "interpark.com" not in self._url():
                try:
                    self.log("→ 놀 인터파크 메인(nol.interpark.com)으로 이동")
                    self.driver.get("https://nol.interpark.com/")
                    self._wait(2.0)
                except:
                    self._wait(1)

            # ② 예매 페이지 대기 (예매창은 새 탭/같은 탭 어디서든 열릴 수 있다)
            # 매초 모든 탭의 주소를 전환 없이 조회해, 예매창이 보이면 한 번만 전환한다.
            self.log("원하시는 링크에 들어가서 [예매하기] 버튼을 눌러 주세요.")
            # NOL 인터파크 예매창: 구형 poticket(BookMain.asp) 또는
            # 신형 tickets.interpark.com/onestop/... (예: /onestop/seat)
            booking_kw = ("poticket", "Book", "/onestop")
            for _ in range(1800):
                self._wait(1)
                if self._switch_to_booking_window(booking_kw):
                    self.log("→ 예매창으로 전환 완료")
                    break
            else:
                self.log("예매창 대기 시간 초과"); return
            self._wait(2)

            # ③ 안심예매 보안문자: 예매창에서 직접 입력 → 통과 대기
            if self._captcha_present():
                self._wait_captcha_cleared()
                self._wait(1)

            # ④ 좌석 등급 선택 (페이지에서 동적으로 읽음)
            grade, grade_list = self._ask_grade()
            self._wait(0.3)

            # ⑤ 구역 선택 (zones = 구역 dict 리스트)
            zones = self._ask_zones(grade, grade_list)
            zone_labels = [z.get('disp') or z.get('label', '') for z in zones] if zones else []
            self.log(f"선택 구역: {', '.join(zone_labels) if zone_labels else '전체'} "
                     f"({len(zones)}개)")
            self._wait(0.3)

            # ⑥ 구역 순회 + 좌석 클릭
            self._rotate_zones(zones, grade)

            # ⑦ 좌석선택완료
            self._click_complete()

            # ⑦-② 결제 페이지 진입 대기 (가격/할인선택 단계)
            self.log("결제 페이지 진입 대기 중...")
            for _ in range(60):
                self._wait(1)
                if self._is_payment_page():
                    break

            # ⑧ 결제 대기 + 소리 알림
            self._wait_payment()

        except InterruptedError:
            self.log("매크로 중단됨")
        except Exception as e:
            self.log(f"오류: {e}\n{traceback.format_exc()}")


# ───────────────────────────────────────────────
#  컨트롤 창
# ───────────────────────────────────────────────
class ControlWindow(QWidget):
    def __init__(self, driver, delay):
        super().__init__()
        self.setWindowTitle("NOL 인터파크 취소표 매크로")
        self.setFont(QFont("맑은 고딕", 9))
        self.setFixedWidth(310)

        self._sig    = Signals()
        self._thread = MacroThread(driver, delay, self._sig)
        self._paused = False

        self._sig.log_signal.connect(self._on_log)
        self._sig.show_input.connect(self._show_input)
        self._sig.hide_input.connect(self._hide_input)

        root = QVBoxLayout()
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # 로그 박스
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFixedHeight(130)
        self.log_box.setFont(QFont("맑은 고딕", 9))
        self.log_box.setStyleSheet(
            "background:#ffffff; color:#222; border:1px solid #ccc;"
        )
        root.addWidget(self.log_box)

        # 입력 행 (기본 숨김)
        self.input_row = QWidget()
        rl = QHBoxLayout()
        rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(4)
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("보안문자 / 번호 입력")
        self.input_edit.setFixedHeight(28)
        self.input_edit.returnPressed.connect(self._submit)
        self.input_btn = QPushButton("입력완료")
        self.input_btn.setFixedSize(64, 28)
        self.input_btn.setStyleSheet(
            "background:#4a90d9; color:white; font-weight:bold; border-radius:3px;"
        )
        self.input_btn.clicked.connect(self._submit)
        rl.addWidget(self.input_edit); rl.addWidget(self.input_btn)
        self.input_row.setLayout(rl)
        self.input_row.hide()
        root.addWidget(self.input_row)

        # 중단/일시정지 버튼
        bl = QHBoxLayout(); bl.setSpacing(6)
        self.btn_stop = QPushButton("중단하기")
        self.btn_stop.setFixedHeight(32)
        self.btn_stop.setStyleSheet(
            "background:#4a4a4a; color:white; font-weight:bold; border-radius:4px;"
        )
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_pause = QPushButton("일시정지")
        self.btn_pause.setFixedHeight(32)
        self.btn_pause.setStyleSheet(
            "background:#3db06e; color:white; font-weight:bold; border-radius:4px;"
        )
        self.btn_pause.clicked.connect(self._on_pause)
        bl.addWidget(self.btn_stop); bl.addWidget(self.btn_pause)
        root.addLayout(bl)

        self.setLayout(root)
        self.adjustSize()
        self._thread.start()

    def _on_log(self, msg):
        self.log_box.append(msg)
        sb = self.log_box.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _show_input(self):
        self.input_edit.clear()
        self.input_row.show()
        self.input_edit.setFocus()
        self.adjustSize()

    def _hide_input(self):
        self.input_row.hide()
        self.adjustSize()

    def _submit(self):
        self._thread.set_answer(self.input_edit.text().strip())

    def _on_stop(self):
        self._thread.stop()
        self.btn_stop.setEnabled(False)
        self.btn_pause.setEnabled(False)

    def _on_pause(self):
        self._paused = self._thread.toggle_pause()
        if self._paused:
            self.btn_pause.setText("재  개")
            self.btn_pause.setStyleSheet(
                "background:#d0a020; color:white; font-weight:bold; border-radius:4px;"
            )
        else:
            self.btn_pause.setText("일시정지")
            self.btn_pause.setStyleSheet(
                "background:#3db06e; color:white; font-weight:bold; border-radius:4px;"
            )

    def closeEvent(self, event):
        self._thread.stop()
        self._thread.wait(2000)
        event.accept()


# ───────────────────────────────────────────────
#  로그인 창
# ───────────────────────────────────────────────
class LoginWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NOL 인터파크 취소표 매크로")
        self.setFixedWidth(300)
        self.setFont(QFont("맑은 고딕", 10))
        self._ctrl = None

        root = QVBoxLayout()
        root.setSpacing(8)
        root.setContentsMargins(16, 16, 16, 16)

        root.addWidget(QLabel("로그인"))

        # 인터파크 / 카카오 / 네이버 탭
        tab_row = QHBoxLayout(); tab_row.setSpacing(0)
        self._tab_btns = [
            QPushButton("인터파크"),
            QPushButton("카카오"),
            QPushButton("네이버"),
        ]
        self._tab_grp = QButtonGroup(self)
        for i, b in enumerate(self._tab_btns):
            b.setCheckable(True); b.setFixedHeight(30)
            self._tab_grp.addButton(b, i); tab_row.addWidget(b)
        self._tab_btns[0].setChecked(True)
        self._tab_grp.buttonClicked.connect(lambda _: self._update_tabs())
        self._update_tabs()
        root.addLayout(tab_row)

        # ID
        r = QHBoxLayout()
        lb = QLabel("ID"); lb.setFixedWidth(40)
        self.edit_id = QLineEdit(); self.edit_id.setFixedHeight(28)
        r.addWidget(lb); r.addWidget(self.edit_id); root.addLayout(r)

        # PW
        r = QHBoxLayout()
        lb = QLabel("PW"); lb.setFixedWidth(40)
        self.edit_pw = QLineEdit()
        self.edit_pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_pw.setFixedHeight(28)
        r.addWidget(lb); r.addWidget(self.edit_pw); root.addLayout(r)

        # 딜레이
        r = QHBoxLayout()
        lb = QLabel("딜레이"); lb.setFixedWidth(44)
        self.spin = QSpinBox()
        self.spin.setRange(1, 60); self.spin.setValue(1); self.spin.setFixedHeight(28)
        r.addWidget(lb); r.addWidget(self.spin); r.addStretch(); root.addLayout(r)

        # 유효기간
        lbl = QLabel(f"유효기간: {EXPIRE_DATE}")
        lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        lbl.setStyleSheet("color:#888; font-size:9px;")
        root.addWidget(lbl)

        # 시작하기
        self.btn_start = QPushButton("시작하기")
        self.btn_start.setFixedHeight(36)
        self.btn_start.setStyleSheet(
            "background:#3c3c3c; color:white; font-weight:bold;"
            " font-size:12px; border-radius:4px;"
        )
        self.btn_start.clicked.connect(self._on_start)
        root.addWidget(self.btn_start)

        self.setLayout(root)

    def _update_tabs(self):
        for b in self._tab_btns:
            b.setStyleSheet(
                "background:#4a90d9; color:white; font-weight:bold;"
                " border:none; border-radius:3px;" if b.isChecked() else
                "background:#e0e0e0; color:#333; border:none; border-radius:3px;"
            )

    def _on_start(self):
        delay = self.spin.value()
        try:
            opts = webdriver.ChromeOptions()
            opts.add_argument("--disable-blink-features=AutomationControlled")
            opts.add_experimental_option("excludeSwitches", ["enable-automation"])
            opts.add_experimental_option("useAutomationExtension", False)
            # 로그인 세션 유지(자동 로그인): 고정 프로필 폴더 재사용
            # 한 번 로그인하면 쿠키가 이 폴더에 저장돼 다음 실행부터 로그인 유지됨
            profile_dir = os.path.join(
                os.path.expanduser("~"), ".nolinterpark_profile")
            try:
                os.makedirs(profile_dir, exist_ok=True)
            except Exception:
                pass
            opts.add_argument(f"--user-data-dir={profile_dir}")
            opts.add_argument("--profile-directory=Default")
            drv = webdriver.Chrome(
                service=Service(ChromeDriverManager().install()), options=opts
            )
            drv.set_page_load_timeout(30)
            drv.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"}
            )
        except Exception as e:
            QMessageBox.critical(
                self, "오류",
                f"Chrome 실행 실패:\n{e}\n\n"
                f"(Chrome 창이 이미 열려 있으면 모두 닫고 다시 시도하세요)"); return

        try: drv.get(LOGIN_URL)
        except: pass

        self._ctrl = ControlWindow(drv, delay)
        self._ctrl.show()
        self.hide()


# ───────────────────────────────────────────────
#  진입점
# ───────────────────────────────────────────────
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setFont(QFont("맑은 고딕", 10))
    win = LoginWindow()
    win.show()
    sys.exit(app.exec())

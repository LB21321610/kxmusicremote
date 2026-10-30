#!/usr/bin/env python3
"""Apple Music Remote for Windows.

A tiny self-hosted web remote for the Apple Music app on Windows. Serves a
mobile-friendly web UI on your local network with now-playing info, transport
controls, volume, in-track seeking, shuffle/repeat, and a playlist browser.

The Apple Music app for Windows does not support Apple's iTunes Remote app
(it exposes no DACP service), so this project bridges the gap with three
Windows-native mechanisms:

- SMTC (System Media Transport Controls, via ``winsdk``) for playback state,
  track metadata, artwork, and play/pause/next/previous.
- Core Audio (via ``pycaw``) for reading and setting the system volume.
- UI Automation (via ``uiautomation``) for everything Apple Music's SMTC
  integration ignores: seeking, shuffle, repeat, and playlist browsing.

Usage (must run on Windows with the Apple Music app installed)::

    pip install -r requirements.txt
    python apple-music-remote.py [--port 8765]

Then open the printed URL in your phone's browser (same Wi-Fi network).
See README.md for firewall setup and limitations.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import socket
import threading
import time
import unicodedata
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator, Optional
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
from lyrics_translation import service as translation_service
from lyrics_multisource import service as multisource_lyrics
from lyrics_matching import approximate_score, score_record
from remote_settings import SettingsStore
import apple_catalog

import uiautomation
from comtypes import CoInitialize
from pycaw.pycaw import AudioUtilities
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSession as Session,
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winsdk.windows.storage.streams import Buffer, DataReader, InputStreamOptions

DEFAULT_PORT = 8765
APPLE_MUSIC_WINDOW = "Apple Music"
SCRUBBER_AUTOMATION_ID = "LCDScrubber"
MINI_SCRUBBER_AUTOMATION_ID = "Scrubber"
TRACK_DURATION_RE = re.compile(r"^\d+:\d{2}$")
LYRIC_SETTINGS = SettingsStore()

# Repeat state is read from the transport-bar button's accessible name.
# English and Chinese accessible names; other locales fall back to "none".
REPEAT_STATES = {"Do Not Repeat": "none", "Repeat All": "all", "Repeat One": "one",
                 "不重复播放": "none", "全部重复播放": "all", "单曲重复播放": "one",
                 "不重複播放": "none", "全部重複播放": "all", "單曲重複播放": "one",
                 # Apple Music 1.1792 names
                 "不重复": "none", "重复播放一首": "one", "不重複": "none", "重複播放一首": "one"}

PAGE = """<!doctype html><html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=apple-mobile-web-app-capable content=yes>
<meta name=apple-mobile-web-app-status-bar-style content=black-translucent>
<meta name=theme-color content="#000">
<meta name=color-scheme content=dark>
<title>Apple Music Remote</title>
<style>
/* 2026 Apple Music look: black and white, no red; switches use system green. */
:root{--accent:#fff;--focus:rgba(255,255,255,.92);--switch-on:#30d158;--t2:rgba(255,255,255,.68);--t3:rgba(255,255,255,.45);--glass:rgba(255,255,255,.13);
  --glass-hi:rgba(255,255,255,.22);--ease:cubic-bezier(.22,1,.36,1);--spring:cubic-bezier(.3,1.4,.5,1);
  --glass-fill:linear-gradient(180deg,rgba(255,255,255,.2),rgba(255,255,255,.07));
  --glass-edge:inset 0 1px .5px rgba(255,255,255,.42),inset 0 -.5px .5px rgba(255,255,255,.12),inset 0 0 0 .5px rgba(255,255,255,.16);
  --font:-apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue","PingFang SC","Hiragino Sans GB","Microsoft YaHei UI","Microsoft YaHei","Segoe UI",sans-serif}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent;user-select:none;-webkit-user-select:none}
html,body{height:100%}
body{font-family:var(--font);color:#fff;background:#000;overflow:hidden;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
/* Flowing artwork backdrop: a tiny canvas scaled up by the compositor, grain against banding. */
#bg{position:fixed;inset:0;overflow:hidden;background:#161618}
#bg-canvas,#bg-gl{position:absolute;inset:0;width:100%;height:100%;display:block}
/* Motion artwork: full-screen tall video on phones in portrait, square video over #art elsewhere. */
#bg-motion{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:0;transition:opacity .8s ease;pointer-events:none}
#bg-motion.on{opacity:1}
#bg-shade{position:absolute;inset:0;opacity:0;transition:opacity .8s ease;pointer-events:none;
  background:linear-gradient(180deg,rgba(0,0,0,.3) 0,rgba(0,0,0,0) 20%,rgba(0,0,0,0) 40%,rgba(0,0,0,.52) 64%,rgba(0,0,0,.84) 100%)}
body.motion-tall #bg-shade{opacity:1}
body.motion-tall:not(.lyrics-visible) #art{visibility:hidden}
#art-motion{position:fixed;z-index:1;left:0;top:0;width:0;height:0;border-radius:14px;object-fit:cover;opacity:0;
  pointer-events:none;transition:opacity .6s ease}
#art-motion.on{opacity:1}
#bg::after{content:'';position:absolute;inset:0;pointer-events:none;
  background:radial-gradient(130% 90% at 50% 35%,transparent 40%,rgba(0,0,0,.3)),url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3' stitchTiles='stitch'/%3E%3CfeColorMatrix values='0 0 0 0 1 0 0 0 0 1 0 0 0 0 1 0 0 0 .06 0'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}
header{position:fixed;top:0;left:0;right:0;z-index:10;display:flex;align-items:center;gap:8px;
  padding:calc(env(safe-area-inset-top,0px) + 10px) max(12px,env(safe-area-inset-right,0px)) 8px max(12px,env(safe-area-inset-left,0px))}
button{background:none;border:none;color:#fff;padding:6px;cursor:pointer;font:inherit;position:relative;
  transition:transform .5s var(--spring),opacity .2s,background-color .25s,color .25s,box-shadow .25s}
button:active{transform:scale(.88)}
button:disabled{opacity:.4;cursor:default;transform:none}
button svg{width:32px;height:32px;fill:currentColor;display:block;pointer-events:none}
.stroke{fill:none!important;stroke:currentColor;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}
header button{display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;padding:0;flex-shrink:0;
  border-radius:50%;background:var(--glass-fill);-webkit-backdrop-filter:blur(20px) saturate(1.8);backdrop-filter:blur(20px) saturate(1.8);
  box-shadow:var(--glass-edge),0 6px 16px rgba(0,0,0,.16)}
header svg{width:22px;height:22px}
#hright{display:flex;align-items:center;gap:10px;flex-shrink:0;margin-left:auto}
#htitle{flex:1;min-width:0;text-align:center;font-weight:700;font-size:1.06rem;letter-spacing:-.01em;white-space:nowrap;
        overflow:hidden;text-overflow:ellipsis;position:absolute;left:72px;right:72px;pointer-events:none}
.hspace{width:44px;flex-shrink:0}
#lyric-toggle[aria-pressed=true]{background:rgba(255,255,255,.94);color:#18181b;box-shadow:0 6px 20px rgba(0,0,0,.28)}
.view{position:fixed;inset:0;display:flex;flex-direction:column;
      transition:opacity .4s var(--ease),transform .6s var(--ease),visibility 0s}
.view.off{opacity:0;pointer-events:none;visibility:hidden;
      transition:opacity .22s ease,transform .45s var(--ease),visibility 0s .45s}
/* #view-now only fades: the fixed lyrics overlay inside it is positioned from
   measured rects, which a transform would skew mid-transition. */
#view-que.off,#view-src.off,#view-alb.off,#view-art.off{transform:translateY(28px)}
#view-now{align-items:center;justify-content:center;
  padding:calc(env(safe-area-inset-top,0px) + 66px) max(16px,env(safe-area-inset-right,0px)) calc(env(safe-area-inset-bottom,0px) + 18px) max(16px,env(safe-area-inset-left,0px))}
/* Keep main's five children and #art intact for the WKWebView layout injector. */
#view-now main{width:min(88vw,420px);height:100%;max-height:800px;min-height:0;display:grid;
  grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(0,1fr) auto auto auto auto;gap:clamp(10px,2.2vh,22px)}
#view-now main>div{min-width:0}
/* #art fills its grid area while lyrics use it; otherwise script sizes it to the largest square. */
#art{display:block;width:100%;height:100%;max-width:100%;max-height:100%;min-height:0;min-width:0;align-self:center;
  justify-self:center;object-fit:contain;border-radius:14px;background:none;
  box-shadow:0 28px 60px -16px rgba(0,0,0,.62),0 10px 24px -12px rgba(0,0,0,.5);
  transition:transform .75s var(--spring),box-shadow .75s var(--ease)}
#view-now main>#art{object-fit:cover;background:#2c2c2e}
#art.art-missing{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='rgba(255,255,255,.38)' d='M19 3.5v12a3.2 3.2 0 1 1-1.8-2.9V7.3L9.8 8.9v8.6A3.2 3.2 0 1 1 8 14.6V5.4z'/%3E%3C/svg%3E") center/34% no-repeat,linear-gradient(145deg,#4c4c52,#2a2a2e)}
body:not(.playing) #art{transform:scale(.86);box-shadow:0 14px 32px -14px rgba(0,0,0,.55)}
body:not(.lyrics-visible) #art{animation:art-in .6s var(--ease)}
/* Only while the artwork shows: the lyrics overlay measures #art and must never see a scaled box. */
body:not(.lyrics-visible) #art.art-swap{animation:art-swap .7s var(--ease)}
body.lyrics-visible #art{visibility:hidden;transform:none;transition:none}
@keyframes art-in{from{opacity:0;scale:.92}}
@keyframes art-swap{from{opacity:.15;scale:.96}}
.info.swap{animation:info-in .55s var(--ease)}
@keyframes info-in{from{opacity:0;transform:translateY(6px)}}
#t{font-size:1.32rem;font-weight:700;letter-spacing:-.015em;line-height:1.25;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#a{color:var(--t2);margin-top:2px;font-size:1.02rem;line-height:1.3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* Title opens the album, artist opens the artist page (keyboard too). */
#t.linkable,#a.linkable{cursor:pointer;border-radius:6px;transition:opacity .2s}
#prog{visibility:hidden}
#times{display:flex;justify-content:space-between;margin-top:5px;font-size:.72rem;font-weight:600;color:var(--t3);
       font-variant-numeric:tabular-nums;transition:color .25s}
#prog.scrubbing #times{color:var(--t2)}
input[type=range]{--fill:0%;--track:5px;--thumb:14px;display:block;flex:1;min-width:0;width:100%;height:22px;margin:0;
  -webkit-appearance:none;appearance:none;background:transparent;outline:none;cursor:pointer;touch-action:none;transition:opacity .2s}
input[type=range]::-webkit-slider-runnable-track{height:var(--track);border-radius:999px;
  background:linear-gradient(90deg,rgba(255,255,255,.86) var(--fill),rgba(255,255,255,.2) var(--fill));transition:height .25s var(--ease)}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:var(--thumb);height:var(--thumb);border:none;
  margin-top:calc((var(--track) - var(--thumb))/2);border-radius:50%;background:#fff;box-shadow:0 1px 6px rgba(0,0,0,.35);
  transition:transform .35s var(--spring)}
input[type=range]:active::-webkit-slider-thumb{transform:scale(1.3)}
input[type=range]::-moz-range-track{height:var(--track);border-radius:999px;background:rgba(255,255,255,.2)}
input[type=range]::-moz-range-progress{height:var(--track);border-radius:999px;background:rgba(255,255,255,.86)}
input[type=range]::-moz-range-thumb{width:var(--thumb);height:var(--thumb);border:none;border-radius:50%;background:#fff}
#pr{--track:5px;--thumb:13px}
#pr:active,#prog.scrubbing #pr{--track:9px}
#vr{--track:6px;--thumb:20px}
#vr:active{--track:8px}
#ctrl{display:flex;align-items:center;justify-content:space-between;width:100%;max-width:330px;margin:0 auto}
#ctrl button{display:grid;place-items:center;border-radius:50%}
#ctrl button::before{content:'';position:absolute;inset:0;border-radius:inherit;background:rgba(255,255,255,.14);opacity:0;
  transform:scale(.6);transition:opacity .3s,transform .45s var(--spring)}
#ctrl button:active::before{opacity:1;transform:none}
#ctrl button:active{transform:scale(.86)}
#ctrl .prev svg,#ctrl .next svg{width:34px;height:34px;stroke:currentColor;stroke-width:1.3;stroke-linejoin:round}
#ctrl .prev svg{transform:scaleX(-1)}
#pp{width:72px;height:72px}
#pp svg{width:52px;height:52px}
#pp .ic{grid-area:1/1;transition:opacity .2s ease,transform .5s var(--spring)}
body:not(.playing) #pp .ic-pause,body.playing #pp .ic-play{opacity:0;transform:scale(.4)}
#ctrl #shf,#ctrl #rpt{width:40px;height:40px;padding:0;border-radius:12px;transition:transform .5s var(--spring),background-color .3s}
#shf svg,#rpt svg{width:21px;height:21px;fill:var(--t3);transition:fill .25s}
#ctrl #shf.on,#ctrl #rpt.on{background:rgba(255,255,255,.16)}
#shf.on svg,#rpt.on svg{fill:#fff}
#vol{display:flex;align-items:center;gap:12px;color:var(--t3)}
#vol svg{width:18px;height:18px;fill:currentColor;display:block;flex:none}
#mute{padding:8px;margin:-8px;border-radius:50%;color:var(--t3)}
#mute .ic-x{fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;opacity:0;transition:opacity .2s}
body.muted #mute .ic-x{opacity:1}
body.muted #vol input{opacity:.35}
#sq{width:100%;padding:12px 16px;border-radius:12px;border:none;outline:none;font-size:1rem;
    background:rgba(255,255,255,.12);color:#fff;margin-bottom:12px;-webkit-appearance:none;font-family:inherit}
#view-pls,#view-trs,#view-src,#view-que,#view-alb{padding-top:calc(env(safe-area-inset-top,0px) + 66px)}
.listwrap{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;overscroll-behavior:contain;width:100%;max-width:560px;
          margin:0 auto;padding:4px 16px calc(env(safe-area-inset-bottom,0px) + 30px)}
.row{display:flex;align-items:center;gap:14px;padding:11px 14px;border-radius:14px;background:rgba(255,255,255,.08);
     margin-bottom:6px;cursor:default;transition:background-color .2s;
     animation:row-in .55s var(--ease) backwards;animation-delay:calc(min(var(--i,0),14)*32ms)}
@keyframes row-in{from{opacity:0;transform:translateY(12px)}}
.row.busy{opacity:.45}
button.row{width:100%;border:0;color:inherit;font:inherit;text-align:left;cursor:pointer}
button.row:disabled{cursor:wait}
.qidx{width:22px;flex:none;text-align:center;font-size:.8rem;font-weight:700;color:var(--t3);font-variant-numeric:tabular-nums}
.badge{width:44px;height:44px;border-radius:11px;display:flex;align-items:center;justify-content:center;
       font-weight:700;font-size:1.15rem;background:linear-gradient(135deg,#6b6b72,#3a3a40);flex-shrink:0}
.rt{flex:1;min-width:0}
.rt b{display:block;font-size:.95rem;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rt span{display:block;font-size:.8rem;color:var(--t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
.row.playing b{color:#fff;font-weight:700}
.dur{font-size:.78rem;color:var(--t3);font-variant-numeric:tabular-nums;flex-shrink:0}
.chev{width:14px;height:14px;fill:rgba(255,255,255,.4);flex-shrink:0}
.spin{width:28px;height:28px;border:2.5px solid rgba(255,255,255,.18);border-top-color:#fff;
      border-radius:50%;animation:r .8s linear infinite;margin:44px auto 14px}
@keyframes r{to{transform:rotate(360deg)}}
.hint{text-align:center;color:var(--t2);font-size:.88rem;padding:40px 30px 0}
button:focus-visible,input:focus-visible,select:focus-visible,#lyrics-scroll:focus-visible,
#t.linkable:focus-visible,#a.linkable:focus-visible{outline:2px solid var(--focus);outline-offset:3px}
[hidden]{display:none!important}
#lyrics-panel{position:fixed;z-index:2;display:flex;flex-direction:column;min-width:0;min-height:0;
  width:0;height:0;border-radius:22px;background:linear-gradient(180deg,rgba(0,0,0,.2),rgba(0,0,0,.1));overflow:hidden;
  text-align:left;container-type:inline-size;box-shadow:inset 0 0 0 .5px rgba(255,255,255,.08)}
/* The panel box itself never animates: its geometry mirrors #art and must stay
   exact even if a hidden tab pauses animations. Its contents fade in instead.
   Entrance animations must not fill forwards either: a lingering opacity effect
   turns the element into a backdrop root and silently disables nested blur. */
#lyrics-panel:not([hidden])>*{animation:panel-in .45s var(--ease)}
@keyframes panel-in{from{opacity:0}}
#view-now.off #lyrics-panel{pointer-events:none}
#lyrics-source{flex:none;display:flex;align-items:center;gap:10px;min-height:52px;padding:10px 10px 6px 18px}
#lyrics-meta{flex:1;min-width:0;display:flex;align-items:baseline;gap:6px;font-size:.72rem;font-weight:600;
  color:var(--t3);white-space:nowrap;overflow:hidden}
#lyrics-meta>span{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis}
#lyrics-source-label{color:var(--t2);flex-shrink:0!important}
#lyrics-status:not(:empty)::before{content:'·';margin-right:6px}
#lyrics-tune{flex:none;display:inline-flex;align-items:center;gap:5px;height:32px;padding:0 13px 0 10px;border-radius:16px;
  font-size:.78rem;font-weight:600;background:var(--glass-fill);-webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);box-shadow:var(--glass-edge)}
#lyrics-tune svg{width:16px;height:16px}
#lyrics-tune[aria-expanded=true]{background:rgba(255,255,255,.94);color:#18181b}
#lyrics-tune.attention::after{content:'';position:absolute;top:4px;right:5px;width:7px;height:7px;border-radius:50%;
  background:var(--accent);box-shadow:0 0 0 2px rgba(0,0,0,.25)}
#lyrics-sheet{position:absolute;z-index:4;top:54px;right:10px;width:min(340px,calc(100% - 20px));max-height:calc(100% - 64px);
  overflow:auto;overscroll-behavior:contain;display:flex;flex-direction:column;gap:14px;padding:14px;border-radius:18px;
  background:rgba(36,36,40,.74);-webkit-backdrop-filter:blur(30px) saturate(1.8);backdrop-filter:blur(30px) saturate(1.8);
  box-shadow:0 18px 50px rgba(0,0,0,.45),inset 0 0 0 .5px rgba(255,255,255,.12);transform-origin:top right}
#lyrics-sheet:not([hidden]){animation:sheet-in .36s var(--spring)}
#lyrics-sheet{scrollbar-width:thin;scrollbar-color:rgba(255,255,255,.25) transparent}
#lyrics-sheet::-webkit-scrollbar{width:6px;background:transparent}
#lyrics-sheet::-webkit-scrollbar-thumb{background:rgba(255,255,255,.25);border-radius:999px}
#lyrics-sheet::-webkit-scrollbar-button{display:none;width:0;height:0}
@keyframes sheet-in{from{opacity:0;transform:translateY(-6px) scale(.9)}}
.sheet-row{display:flex;align-items:center;justify-content:space-between;gap:12px}
.sheet-label{font-size:.86rem;font-weight:600}
.sheet-sub{font-size:.7rem;line-height:1.35;color:var(--t3);margin-top:2px}
.sheet-title{font-size:.68rem;font-weight:700;letter-spacing:.04em;color:var(--t3)}
#translation-toggle,#motion-toggle{flex:none;width:51px;height:31px;padding:0;border-radius:16px;background:rgba(120,120,128,.4)}
#translation-toggle::after,#motion-toggle::after{content:'';position:absolute;top:2px;left:2px;width:27px;height:27px;border-radius:50%;background:#fff;
  box-shadow:0 3px 8px rgba(0,0,0,.28);transition:transform .38s var(--spring)}
#translation-toggle[aria-pressed=true],#motion-toggle[aria-pressed=true]{background:var(--switch-on)}
#translation-toggle[aria-pressed=true]::after,#motion-toggle[aria-pressed=true]::after{transform:translateX(20px)}
#lyrics-options{display:flex;flex-direction:column;gap:8px}
#lyrics-version{width:100%;min-width:0;-webkit-appearance:none;appearance:none;font:inherit;font-size:.8rem;color:#fff;border:none;
  border-radius:10px;padding:9px 32px 9px 12px;outline:none;cursor:pointer;
  background:rgba(255,255,255,.1) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='none' stroke='white' stroke-width='2.4' stroke-linecap='round' stroke-linejoin='round' d='M7 10l5 5 5-5'/%3E%3C/svg%3E") right 10px center/14px no-repeat}
#lyrics-version option{background:#2c2c30;color:#fff}
#lyrics-options .sheet-title:not(:first-child){margin-top:6px}
#lyrics-offset-controls{display:flex;align-items:center;gap:6px}
#lyrics-offset-controls button{font-size:.78rem;font-weight:600;min-height:34px;padding:0 12px;border-radius:10px;background:rgba(255,255,255,.14)}
#lyrics-offset{flex:1;min-width:64px;text-align:center;font-size:.82rem;font-weight:700;font-variant-numeric:tabular-nums}
#lyrics-offset-status{font-size:.7rem;line-height:1.35;color:var(--t3)}
#translation-status{flex:none;font-size:.7rem;line-height:1.3;padding:0 18px 6px;color:var(--t3);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#lyrics-scroll{position:relative;flex:1;min-height:0;overflow-y:auto;overflow-x:hidden;overscroll-behavior:contain;
  -webkit-overflow-scrolling:touch;touch-action:pan-y;padding:0 20px;padding:0 clamp(16px,6cqi,30px);scroll-behavior:auto;
  scrollbar-width:thin;scrollbar-color:rgba(255,255,255,.22) transparent;
  -webkit-mask-image:linear-gradient(transparent,#000 8%,#000 86%,transparent);mask-image:linear-gradient(transparent,#000 8%,#000 86%,transparent)}
#lyrics-scroll::-webkit-scrollbar{width:6px;background:transparent}
#lyrics-scroll::-webkit-scrollbar-track{background:transparent}
#lyrics-scroll::-webkit-scrollbar-thumb{background:rgba(255,255,255,.22);border-radius:999px}
#lyrics-scroll::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,.4)}
#lyrics-scroll::-webkit-scrollbar-button{display:none;width:0;height:0}
#lyrics-scroll::after{content:'';display:block;height:var(--lyric-tail,120px)}
#lyrics-scroll> :first-child{margin-top:var(--lyric-head,12px)}
#lyrics-scroll:empty::after{display:none}
#lyrics-scroll:empty::before{content:attr(data-empty);display:flex;align-items:center;justify-content:center;height:100%;
  padding:0 16px 8%;text-align:center;font-size:1.05rem;font-weight:600;line-height:1.5;color:var(--t2)}
#lyrics-panel[data-state=loading] #lyrics-scroll:empty::before{animation:breathe 1.6s ease-in-out infinite}
@keyframes breathe{50%{opacity:.4}}
/* Apple Music-style lines: the current line is bright, distant lines dim and soften by --d. */
/* pre-line keeps paired-line breaks but hides the leading spaces many LRC sources carry. */
.lyric-line{font-size:1.45rem;font-size:clamp(1.2rem,6.4cqi,2.15rem);font-weight:700;line-height:1.3;letter-spacing:-.012em;
  white-space:pre-line;overflow-wrap:anywhere;word-break:normal;margin:0 0 .85em;color:rgba(255,255,255,.3);
  filter:blur(calc(min(var(--d,6),6)*.4px));
  transition:translate .8s var(--ease) var(--delay,0ms),opacity .5s ease var(--delay,0ms),color .45s ease,filter .5s ease}
.lyric-line.current{color:#fff;filter:none}
.lyric-original,.lyric-translation{display:block}
.lyric-original{transform-origin:left center;transform:scale(.965);transition:transform .6s var(--spring)}
.lyric-line.current .lyric-original{transform:none}
.lyric-translation{font-size:.62em;font-weight:600;line-height:1.4;margin-top:.32em;opacity:.8;letter-spacing:0}
#lyrics-panel.browsing .lyric-line{filter:none;color:rgba(255,255,255,.55)}
#lyrics-panel.browsing .lyric-line.current{color:#fff}
.lyric-gap .lyric-original{font-size:0;height:22px;display:flex;align-items:center}
.lyric-gap .lyric-original::before{content:'';display:block;width:46px;height:12px;
  background:radial-gradient(circle,currentColor 5px,transparent 5.5px) 0 0/16px 12px repeat-x}
.lyric-gap.current .lyric-original::before{animation:dots 1.8s ease-in-out infinite}
@keyframes dots{50%{transform:scale(1.16);opacity:.7}}
.lyrics-plain{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.6;font-size:1rem;
  user-select:text;-webkit-user-select:text}
.lyrics-plain .lyric-line{font-size:1.15rem;font-size:clamp(1rem,4.6cqi,1.45rem);font-weight:600;color:rgba(255,255,255,.84);
  filter:none;margin-bottom:.7em}
.lyrics-plain .lyric-original{transform:none}
#lyrics-actions{position:absolute;z-index:3;left:0;right:0;bottom:14px;display:flex;justify-content:center;gap:8px;
  padding:0 10px;pointer-events:none}
#lyrics-actions button{pointer-events:auto;font-size:.8rem;font-weight:600;min-height:36px;padding:0 16px;border-radius:18px;
  background:var(--glass-fill);-webkit-backdrop-filter:blur(24px) saturate(1.6);backdrop-filter:blur(24px) saturate(1.6);
  box-shadow:var(--glass-edge),0 8px 24px rgba(0,0,0,.25);animation:pop-in .45s var(--spring)}
#control-error{position:fixed;z-index:100;bottom:calc(env(safe-area-inset-bottom,0px) + 14px);left:50%;transform:translateX(-50%);
  width:max-content;max-width:calc(100vw - 32px);padding:12px 18px;border-radius:16px;background:rgba(44,44,48,.94);color:#fff;font-size:.85rem;
  -webkit-backdrop-filter:blur(24px);backdrop-filter:blur(24px);box-shadow:inset 0 0 0 .5px rgba(255,255,255,.16),0 4px 24px #0008}
/* Catalog: search, album and artist pages */
#src-wrap{max-width:640px}
#src-form{margin:0}
#sq::placeholder{color:var(--t3)}
#src-kinds{display:flex;gap:4px;margin:0 0 14px;padding:3px;border-radius:12px;background:rgba(255,255,255,.08)}
#src-kinds button{flex:1;padding:8px 0;border-radius:9px;font-size:.86rem;font-weight:600;color:var(--t2)}
#src-kinds button[aria-selected=true]{background:rgba(255,255,255,.92);color:#111}
#src-status .hint{padding-top:30px}
#src-status .pill{display:flex;margin:14px auto 0}
.row-wrap{display:flex;gap:6px;align-items:stretch;margin-bottom:6px}
.row-wrap .row{flex:1;min-width:0;margin-bottom:0}
.row-more{flex:none;width:44px;border-radius:14px;background:rgba(255,255,255,.08);display:grid;place-items:center}
.row-more .chev{width:16px;height:16px}
.thumb{width:48px;height:48px;border-radius:8px;object-fit:cover;flex:none;background:rgba(255,255,255,.1)}
.avatar{width:48px;height:48px;border-radius:50%;flex:none;display:grid;place-items:center;font-weight:700;font-size:1.1rem;
  background:linear-gradient(145deg,#5a5a60,#2c2c30)}
.explicit{flex:none;display:inline-grid;place-items:center;min-width:15px;height:15px;padding:0 3px;border-radius:3px;
  background:rgba(255,255,255,.3);color:#000;font-size:.6rem;font-weight:800}
.recent-icon svg{width:18px;height:18px;fill:var(--t3)}
.section-head{display:flex;align-items:center;justify-content:space-between;margin:24px 0 10px;font-size:1.15rem;font-weight:700;letter-spacing:-.01em}
.link{color:var(--t2);font-size:.86rem;font-weight:600;padding:4px 2px}
.pill{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:46px;padding:0 20px;border-radius:999px;
  font-weight:600;font-size:.95rem;background:var(--glass-fill);-webkit-backdrop-filter:blur(20px) saturate(1.6);
  backdrop-filter:blur(20px) saturate(1.6);box-shadow:var(--glass-edge)}
.pill svg{width:18px;height:18px}
.pill.busy,.track.busy,.row.busy{opacity:.5}
.listwrap{scrollbar-width:thin;scrollbar-color:rgba(255,255,255,.22) transparent}
.listwrap::-webkit-scrollbar{width:6px;background:transparent}
.listwrap::-webkit-scrollbar-thumb{background:rgba(255,255,255,.22);border-radius:999px}
.listwrap::-webkit-scrollbar-button{display:none;width:0;height:0}
.listwrap.catalog{max-width:720px;-webkit-mask-image:linear-gradient(transparent 0,#000 18px);mask-image:linear-gradient(transparent 0,#000 18px)}
#view-alb{background:linear-gradient(180deg,var(--tint,#232326) 0,var(--tint,#232326) 160px,#0c0c0e 620px)}
#view-art{background:#0c0c0e}
/* The artist gradient scrolls with the header image so the two never separate. */
.art-tail{display:flow-root;min-height:60vh;background:linear-gradient(180deg,var(--tint,#232326) 0,#0c0c0e 460px)}
.alb-hero{display:flex;flex-direction:column;align-items:center;text-align:center;padding:6px 0 4px}
.alb-art{position:relative;width:min(70vw,320px);aspect-ratio:1;border-radius:12px;overflow:hidden;background:#2c2c2e;
  box-shadow:0 30px 60px -20px rgba(0,0,0,.7),0 12px 28px -14px rgba(0,0,0,.55)}
.alb-art img,.alb-art video{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.alb-art video{opacity:0;transition:opacity .6s ease}
.alb-art video.on{opacity:1}
.page-title{margin-top:18px;font-size:1.5rem;font-weight:700;letter-spacing:-.02em;line-height:1.2;outline:none;overflow-wrap:anywhere}
.page-artists{margin-top:4px;font-size:1.08rem;color:var(--t2);overflow-wrap:anywhere}
.page-artists .link{font-size:inherit;color:inherit;font-weight:500;padding:2px}
.page-sub{margin-top:4px;font-size:.8rem;font-weight:600;color:var(--t3)}
.alb-actions{display:flex;gap:12px;width:min(100%,420px);margin:18px auto 6px}
.alb-actions .pill{flex:1;padding:0 10px}
.expand{margin:16px 0 4px;color:var(--t2);font-size:.9rem;line-height:1.55;white-space:pre-line;text-align:left}
.expand .body{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:var(--lines,3);line-clamp:var(--lines,3);overflow:hidden}
.expand.open .body{display:block}
.expand button{margin-top:4px;padding:2px 0;color:#fff;font-weight:600;font-size:.86rem}
.tracks{margin-top:10px}
.track{display:flex;align-items:center;gap:12px;width:100%;padding:11px 6px;border-radius:10px;text-align:left;color:inherit;
  box-shadow:inset 0 -.5px 0 rgba(255,255,255,.1)}
.track:disabled{opacity:.35}
.track .tn{width:24px;flex:none;text-align:center;color:var(--t3);font-size:.9rem;font-variant-numeric:tabular-nums}
.track .rt b{font-weight:500}
.track.current .rt b{font-weight:700}
.np-bars{display:none;align-items:flex-end;justify-content:center;gap:2px;width:14px;height:12px;margin:0 auto;color:#fff}
.np-bars i{width:3px;height:40%;border-radius:1px;background:currentColor;animation:np 1s ease-in-out infinite}
.np-bars i:nth-child(2){animation-delay:-.4s}.np-bars i:nth-child(3){animation-delay:-.7s}
@keyframes np{0%,100%{height:30%}50%{height:100%}}
body:not(.playing) .np-bars i{animation-play-state:paused}
.track.current .num{display:none}
.track.current .np-bars{display:flex}
.track.flash{animation:flash 1.6s ease}
@keyframes flash{20%{background:rgba(255,255,255,.14)}}
.disc{margin:18px 6px 4px;font-size:.78rem;font-weight:700;color:var(--t3)}
.alb-foot{margin:22px 0 8px;font-size:.78rem;line-height:1.6;color:var(--t3)}
.alb-foot .pill{margin-top:14px;min-height:40px;font-size:.86rem}
.shelf-row{display:flex;gap:14px;overflow-x:auto;overscroll-behavior-x:contain;scroll-snap-type:x proximity;
  margin:0 -16px;padding:0 16px 8px;scrollbar-width:none}
.shelf-row::-webkit-scrollbar{display:none}
.tile{flex:none;display:flex;flex-direction:column;gap:6px;width:140px;padding:0;border-radius:8px;text-align:left;scroll-snap-align:start}
.tile-art{width:140px;height:140px;border-radius:8px;object-fit:cover;background:rgba(255,255,255,.08)}
.tile-title{font-size:.85rem;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile-sub{margin-top:-4px;font-size:.75rem;color:var(--t3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile.circle{width:108px;align-items:center;text-align:center}
.tile.circle .tile-art{width:108px;height:108px;border-radius:50%}
.tile.circle .tile-title{max-width:100%}
.feature{display:flex;align-items:center;gap:14px;width:100%;margin-top:6px;padding:12px;border-radius:16px;text-align:left;
  background:rgba(255,255,255,.08)}
.feature .tile-art{width:84px;height:84px}
.feature small{display:block;font-size:.72rem;font-weight:700;color:var(--t3);margin-bottom:2px}
#view-art{padding-top:0}
#art-body.listwrap{max-width:none;padding:0 0 calc(env(safe-area-inset-bottom,0px) + 30px)}
.page-inner{max-width:720px;margin:0 auto;padding:0 16px}
.art-hero{position:relative;height:min(52vh,440px);min-height:250px;overflow:hidden;display:flex;align-items:flex-end;background:var(--tint,#232326)}
.art-hero img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;object-position:center 28%}
.art-hero::after{content:'';position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(180deg,rgba(0,0,0,.28) 0,rgba(0,0,0,0) 26%,rgba(0,0,0,0) 48%,var(--tint,#232326) 100%)}
.art-hero.no-image{background:radial-gradient(120% 90% at 50% 18%,var(--tint-hi,#55555a),var(--tint,#232326))}
.art-initial{position:absolute;inset:0;display:grid;place-items:center;font-size:7rem;font-weight:800;color:rgba(255,255,255,.16)}
.art-hero .page-title{position:relative;z-index:1;width:100%;max-width:720px;margin:0 auto;padding:0 16px 14px;font-size:2.15rem;
  text-shadow:0 2px 18px rgba(0,0,0,.35)}
#artist-choice{position:fixed;inset:0;z-index:60;display:flex;align-items:flex-end;justify-content:center;background:rgba(0,0,0,.35)}
.choice-sheet{width:min(calc(100% - 24px),420px);margin-bottom:calc(env(safe-area-inset-bottom,0px) + 12px);padding:14px;border-radius:22px;
  background:rgba(36,36,40,.84);-webkit-backdrop-filter:blur(30px) saturate(1.8);backdrop-filter:blur(30px) saturate(1.8);
  box-shadow:0 18px 50px rgba(0,0,0,.45),inset 0 0 0 .5px rgba(255,255,255,.12);animation:sheet-in .36s var(--spring)}
.choice-title{margin:2px 4px 10px;font-size:.78rem;font-weight:700;color:var(--t3)}
.choice-sheet .row{width:100%}
.choice-sheet .pill{width:100%;margin-top:4px}
@keyframes pop-in{from{opacity:0;transform:scale(.9)}}
@media(hover:hover){
  header button:hover{background:var(--glass-hi)}
  #lyric-toggle[aria-pressed=true]:hover{background:#fff}
  #ctrl button:hover::before{opacity:.75;transform:none}
  #pr:hover{--track:7px}#vr:hover{--track:8px}
  .row:hover{background:rgba(255,255,255,.13)}
  #lyrics-tune:not([aria-expanded=true]):hover,#lyrics-actions button:hover,#lyrics-offset-controls button:hover:not(:disabled){background:rgba(255,255,255,.24)}
  #t.linkable:hover,#a.linkable:hover{opacity:.78}
  .track:hover:not(:disabled),.row-more:hover,.feature:hover{background:rgba(255,255,255,.1)}
  .pill:hover:not(:disabled){background:rgba(255,255,255,.26)}
  .tile:hover .tile-art{filter:brightness(1.12)}
  #src-kinds button:not([aria-selected=true]):hover{color:#fff}
  .link:hover{color:#fff}
}
@media(min-width:760px) and (min-height:360px){
  #view-now main{width:min(90vw,1000px);max-height:640px;grid-template-columns:minmax(0,1.2fr) minmax(250px,1fr);
    grid-template-rows:minmax(0,1fr) auto auto auto auto minmax(0,1fr);column-gap:clamp(24px,5vw,72px)}
  #view-now main>#art,#view-now main>#remote-art-stage{grid-column:1;grid-row:1 / 7}
  #view-now main>div:nth-child(2){grid-column:2;grid-row:2}
  #prog{grid-column:2;grid-row:3}#ctrl{grid-column:2;grid-row:4}#vol{grid-column:2;grid-row:5}
  #t{font-size:1.5rem}#a{font-size:1.1rem}
}
@media(max-height:520px){
  header{padding-top:calc(env(safe-area-inset-top,0px) + 6px)}
  #view-now{padding-top:calc(env(safe-area-inset-top,0px) + 58px);padding-bottom:calc(env(safe-area-inset-bottom,0px) + 8px)}
  #view-now main{gap:7px}#t{font-size:1rem}#a{font-size:.8rem}
  #ctrl{max-width:270px}#pp{width:54px;height:54px}#pp svg{width:38px;height:38px}
  #ctrl .prev svg,#ctrl .next svg{width:28px;height:28px}#vol{gap:8px}
  #lyrics-source{min-height:40px;padding:4px 8px 2px 14px}
  #lyrics-tune{height:28px;padding:0 10px 0 8px}
  #lyrics-sheet{top:44px;padding:10px;gap:10px}
  #translation-status{font-size:.65rem;line-height:1.15;padding:0 14px 2px}
  #lyrics-actions{bottom:8px}
  #lyrics-actions button{min-height:28px;padding:0 10px;font-size:.72rem}
  .lyric-line{margin-bottom:.6em}
}
@media(min-width:560px) and (max-width:759px) and (max-height:520px){
  #view-now main{width:90vw;grid-template-columns:minmax(0,1.2fr) minmax(220px,1fr);
    grid-template-rows:minmax(0,1fr) auto auto auto auto minmax(0,1fr);column-gap:16px}
  #view-now main>#art,#view-now main>#remote-art-stage{grid-column:1;grid-row:1 / 7}
  #view-now main>div:nth-child(2){grid-column:2;grid-row:2}
  #prog{grid-column:2;grid-row:3}#ctrl{grid-column:2;grid-row:4}#vol{grid-column:2;grid-row:5}
}
@media(prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:1ms!important;animation-delay:0s!important;animation-iteration-count:1!important;
    transition-duration:1ms!important;transition-delay:0s!important}
}
</style>
<div id=bg aria-hidden=true><canvas id=bg-canvas width=64 height=64></canvas><canvas id=bg-gl width=64 height=64 hidden></canvas>
<video id=bg-motion muted playsinline loop preload=none disablepictureinpicture></video><div id=bg-shade></div></div>
<header>
<button id=hbtn type=button aria-label="返回播放页" hidden></button>
<div id=htitle></div>
<span id=hright><button id=lyric-toggle type=button aria-pressed=false aria-controls=lyrics-panel aria-label="歌词" title="歌词" onclick="toggleLyrics()"><svg viewBox="0 0 24 24"><path fill-rule=evenodd d="M7 4h10a4 4 0 0 1 4 4v5a4 4 0 0 1-4 4h-6l-3.4 2.9c-.3.3-.6.1-.6-.3V17a4 4 0 0 1-4-4V8a4 4 0 0 1 4-4zm1.3 4.6a.8.8 0 0 0 0 1.6h7.4a.8.8 0 0 0 0-1.6zm0 3a.8.8 0 0 0 0 1.6h4.4a.8.8 0 0 0 0-1.6z"/></svg></button><button id=qbtn type=button aria-label="待播队列" title="待播队列" onclick="openQue()"></button><button id=sbtn type=button aria-label="搜索" title="搜索" onclick="openSearch()"></button></span>
</header>

<div class=view id=view-now>
<main>
<img id=art alt="">
<div class=info><div id=t role=button tabindex=-1 aria-disabled=true>&mdash;</div><div id=a role=button tabindex=-1 aria-disabled=true></div></div>
<div id=prog>
<input id=pr type=range min=0 max=100 value=0 step=1 aria-label="播放进度" oninput="scrub(this.value)" onchange="seekTo(this.value)">
<div id=times><span id=cur>0:00</span><span id=tot>0:00</span></div></div>
<div id=ctrl>
<button id=shf type=button aria-label="随机播放" onclick="mode('shuffle')"></button>
<button class=prev type=button aria-label="上一首" onclick="c('prev')"><svg viewBox="0 0 24 24"><path d="M3.5 6.8v10.4L11.2 12zM12.3 6.8v10.4L20 12z"/></svg></button>
<button id=pp type=button aria-label="播放／暂停" onclick="c('toggle')"><svg class="ic ic-play" viewBox="0 0 24 24"><path d="M8.2 4.6c-.8-.5-1.7.1-1.7 1v12.8c0 .9.9 1.5 1.7 1l10.4-6.4c.7-.5.7-1.5 0-2z"/></svg><svg class="ic ic-pause" viewBox="0 0 24 24"><path d="M6.4 4.5h2.2c.8 0 1.4.6 1.4 1.4v12.2c0 .8-.6 1.4-1.4 1.4H6.4c-.8 0-1.4-.6-1.4-1.4V5.9c0-.8.6-1.4 1.4-1.4zm9 0h2.2c.8 0 1.4.6 1.4 1.4v12.2c0 .8-.6 1.4-1.4 1.4h-2.2c-.8 0-1.4-.6-1.4-1.4V5.9c0-.8.6-1.4 1.4-1.4z"/></svg></button>
<button class=next type=button aria-label="下一首" onclick="c('next')"><svg viewBox="0 0 24 24"><path d="M3.5 6.8v10.4L11.2 12zM12.3 6.8v10.4L20 12z"/></svg></button>
<button id=rpt type=button aria-label="重复播放" onclick="mode('repeat')"></button>
</div>
<div id=vol>
<button id=mute type=button aria-label="静音" onclick="c('mute')"><svg viewBox="0 0 24 24"><path d="M3 9v6h4l6 5V4L7 9H3z"/><path class=ic-x d="M16.5 9.5l5 5m0-5l-5 5"/></svg></button>
<input id=vr type=range min=0 max=100 value=50 aria-label="音量" oninput="vset(this.value)">
<svg viewBox="0 0 24 24"><path d="M3 9v6h4l6 5V4L7 9H3z"/><path d="M16 8a5.5 5.5 0 010 8l-1.3-1.5a3.5 3.5 0 000-5L16 8z"/><path d="M18.5 5.5a9 9 0 010 13l-1.3-1.5a7 7 0 000-10l1.3-1.5z"/></svg>
</div>
</main>
<section id=lyrics-panel aria-label="歌曲歌词" hidden>
<div id=lyrics-source><span id=lyrics-meta><span id=lyrics-source-label>LDDC 多源 · 第三方歌词</span><span id=lyrics-status role=status aria-live=polite>等待歌曲信息</span></span>
<button id=lyrics-tune type=button aria-expanded=false aria-controls=lyrics-sheet onclick="toggleLyricSheet()"><svg class=stroke viewBox="0 0 24 24"><path d="M4 7h9m4 0h3M4 17h3m4 0h9"/><circle cx="15" cy="7" r="2.2"/><circle cx="9" cy="17" r="2.2"/></svg>调整</button></div>
<div id=lyrics-sheet role=group aria-label="歌词调整" hidden>
<div class=sheet-row><div><div class=sheet-label>简体中文翻译</div><div class=sheet-sub>外语保留原文，下方显示本地 Hy-MT2 译文</div></div>
<button id=translation-toggle type=button aria-pressed=false aria-label="简体中文翻译" onclick="toggleTranslation()"></button></div>
<div class=sheet-row><div><div class=sheet-label>动态封面</div><div class=sheet-sub>专辑支持时播放动态封面；关闭后使用流体背景</div></div>
<button id=motion-toggle type=button aria-pressed=true aria-label="动态封面" onclick="toggleMotion()"></button></div>
<div id=lyrics-options><div class=sheet-title>来源与版本</div>
<select id=lyrics-version aria-label="歌词来源和版本" onchange="changeLyricVersion()"><option value=auto>自动匹配来源／版本</option></select>
<div class=sheet-title>歌词时间 · 所有歌曲</div>
<div id=lyrics-offset-controls aria-label="所有歌曲默认偏移" title="所有歌曲默认偏移 · 所有设备共用"><button type=button disabled onclick="adjustLyricOffset(-.1)" title="歌词提前 0.1 秒">提前</button>
<output id=lyrics-offset aria-live=polite>读取中…</output><button type=button disabled onclick="adjustLyricOffset(.1)" title="歌词延后 0.1 秒">延后</button>
<button type=button disabled onclick="adjustLyricOffset(0,true)">恢复为 0</button></div>
<div id=lyrics-offset-status role=status aria-live=polite>所有歌曲默认偏移 · 正在读取</div></div>
</div>
<div id=translation-status role=status aria-live=polite hidden></div>
<div id=lyrics-scroll tabindex=0 role=region aria-label="可滚动歌词" data-empty="等待歌曲信息"></div>
<div id=lyrics-actions><button id=lyrics-follow type=button hidden onclick="resumeLyrics()">回到当前歌词</button>
<button id=lyrics-retry type=button hidden onclick="requestLyrics()">重试</button>
<button id=translation-retry type=button hidden onclick="startTranslation(true)">重试翻译</button></div>
</section>
<video id=art-motion muted playsinline loop preload=none disablepictureinpicture aria-hidden=true></video>
</div>

<!-- Keep retired library view IDs for existing clients; no navigation or handlers. -->
<div class="view off" id=view-pls hidden><div class=listwrap id=pls-list></div></div>
<div class="view off" id=view-trs hidden><div class=listwrap id=trs-list></div></div>
<!-- Catalog search (Apple Music catalog via iTunes Search; plays in the Windows app). -->
<div class="view off" id=view-src><div class=listwrap id=src-wrap>
<form id=src-form role=search onsubmit="event.preventDefault();runSearch(true)">
<input id=sq type=search enterkeyhint=search autocomplete=off autocapitalize=off spellcheck=false placeholder="歌曲、专辑、艺人" aria-label="搜索 Apple Music 曲库">
</form>
<div id=src-kinds role=tablist aria-label="搜索类型"><button type=button role=tab data-kind=song aria-selected=true>歌曲</button><button type=button role=tab data-kind=album aria-selected=false tabindex=-1>专辑</button><button type=button role=tab data-kind=artist aria-selected=false tabindex=-1>艺人</button></div>
<div id=src-status role=status aria-live=polite></div>
<div id=src-list></div>
</div></div>
<div class="view off" id=view-que><div class=listwrap id=que-list></div></div>
<div class="view off" id=view-alb><div class="listwrap catalog" id=alb-body></div></div>
<div class="view off" id=view-art><div class=listwrap id=art-body></div></div>
<div id=artist-choice hidden><div class=choice-sheet role=dialog aria-modal=true aria-labelledby=artist-choice-title>
<div class=choice-title id=artist-choice-title>选择艺人</div><div id=artist-choice-list></div>
<button type=button class=pill onclick="closeArtistChoice()">取消</button></div></div>
<div id=control-error role=alert hidden></div>

<script>
const $=i=>document.getElementById(i);
const PLAY='<svg viewBox="0 0 24 24"><path d="M7 4v16l14-8z"/></svg>';
const PAUSE='<svg viewBox="0 0 24 24"><path d="M6 4h4.5v16H6zM13.5 4H18v16h-4.5z"/></svg>';
const LISTICON='<svg viewBox="0 0 24 24"><path d="M3 6h12v2H3zM3 11h12v2H3zM3 16h8v2H3zM19 6v8.3a3.2 3.2 0 101.5 2.7V8h2V6h-3.5z"/></svg>';
const BACKICON='<svg viewBox="0 0 24 24"><path d="M15 4l-8 8 8 8 1.6-1.6L10.2 12l6.4-6.4z"/></svg>';
const CHEV='<svg class=chev viewBox="0 0 24 24"><path d="M9 4l8 8-8 8-1.6-1.6L13.8 12 7.4 5.6z"/></svg>';
const SHUF='<svg viewBox="0 0 24 24"><path d="M10.59 9.17 5.41 4 4 5.41l5.17 5.17 1.42-1.41zM14.5 4l2.04 2.04L4 18.59 5.41 20 17.96 7.46 20 9.5V4h-5.5zm.33 9.41-1.42 1.42 3.14 3.14L14.5 20H20v-5.5l-2.04 2.05-3.13-3.14z"/></svg>';
const REP='<svg viewBox="0 0 24 24"><path d="M17 17H7v-3l-4 4 4 4v-3h12v-6h-2v4zM7 7h10v3l4-4-4-4v3H5v6h2V7z"/></svg>';
const REP1='<svg viewBox="0 0 24 24"><path d="M13 15V9h-1l-2 1v1h1.5v4H13zM17 17H7v-3l-4 4 4 4v-3h12v-6h-2v4zM7 7h10v3l4-4-4-4v3H5v6h2V7z"/></svg>';
const SEARCHICON='<svg viewBox="0 0 24 24"><path d="M10 2a8 8 0 105.29 14.03l4.34 4.34 1.41-1.41-4.34-4.34A8 8 0 0010 2zm0 2a6 6 0 110 12 6 6 0 010-12z"/></svg>';
const QUEUEICON='<svg viewBox="0 0 24 24"><path d="M3 5h12v2H3zM3 10h12v2H3zM3 15h7v2H3zM16.5 12.5l6 3.5-6 3.5z"/></svg>';
function esc(s){return(s||'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
let last='',dragTs=0,pos=0,dur=0,posTs=0,playing=false,scrubbing=false,seekTs=0,nowTitle='';
let view='now';
// Catalog pages, current-song match and motion artwork (see the catalog section).
let nowCatalog={key:'',state:'idle',match:null,album:null,promise:null};
let navStack=[],navCurrent=null,catalogPlaying=false,searchKind='song',searchTimer=0,searchSeq=0,searchController=null;
const catalogCache=new Map(),motionFailed=new Set();
let motionWatch=0;
let motionEnabled=true;try{motionEnabled=localStorage.getItem('remote.motionArtwork')!=='false'}catch(e){}
const hlsPlayable=(()=>{try{return !!document.createElement('video').canPlayType('application/vnd.apple.mpegurl')}catch(e){return false}})();
let lyricsVisible=false,lyricTrack=null,lyricKey='',lyricEpoch=0,lyricRequest=null,lyricLoaded=false;
let lyricLines=[],lyricNodes=[],lyricActive=-1,manualUntil=0,layoutFrame=0,layoutTimer=0,pollBusy=false;
let playbackEpoch=0,pendingTrackKey='',pendingTrackUntil=0;
let translationEnabled=false,rawLyrics=null,derivedRows=new Map(),translationEpoch=0,translationRequest=null,translationTimer=0;
let lyricChoice='auto',lyricOffset=0,clockSample='',clockRecalibrate=true;
// Legacy per-song offsets remain stored, but only the server's global value is used.
let offsetConfirmed=null,offsetPending=false,offsetSaving=false,offsetTimer=0,offsetEdit=0,offsetError='';
// Motion is decoration only: state (scrollTop, classes) is always final immediately.
const reducedMotion=window.matchMedia?matchMedia('(prefers-reduced-motion: reduce)'):{matches:false};
function motionOK(){return !reducedMotion.matches&&!document.hidden}
let sheetOpen=false,ppHold=0,artToken=0,artFit='',flipUntil=0;
function renderLyricOffset(){
  $('lyrics-offset').textContent=offsetConfirmed===null?'读取中…':
    lyricOffset===0?'无偏移':(lyricOffset<0?'提前 ':'延后 ')+String(Math.abs(lyricOffset))+' 秒';
  $('lyrics-offset-controls').querySelectorAll('button').forEach(b=>b.disabled=offsetConfirmed===null);
  $('lyrics-offset-status').textContent='所有歌曲默认偏移 · '+
    (offsetError|| (offsetPending||offsetSaving?'保存中…':offsetConfirmed===null?'正在读取':'已保存，所有设备共用'))}
function acceptLyricSettings(data){
  if(!data||!Number.isFinite(data.lyric_offset)||Math.abs(data.lyric_offset)>10||
      !Number.isSafeInteger(data.revision)||data.revision<0)return;
  if(offsetConfirmed&&data.revision<offsetConfirmed.revision)return;
  offsetConfirmed={lyric_offset:data.lyric_offset,revision:data.revision};
  if(!offsetPending&&!offsetSaving){lyricOffset=data.lyric_offset;syncLyrics(playbackPosition(),true)}
  renderLyricOffset()}
async function readLyricSettings(){try{
  const response=await fetch('/settings/lyrics',{cache:'no-store'}),data=await response.json();
  if(!response.ok||!data.ok)throw new Error();if(offsetConfirmed===null)offsetError='';acceptLyricSettings(data);
}catch(e){if(offsetConfirmed===null){offsetError='读取失败，等待重试';renderLyricOffset()}}}
function loadLyricCalibration(){lyricChoice='auto';
  try{const saved=JSON.parse(localStorage.getItem('remote.lyricCalibration.'+lyricKey)||'{}');
    lyricChoice=typeof saved.candidate==='string'?saved.candidate:'auto'}catch(e){}
  $('lyrics-version').value='auto'}
function saveLyricCalibration(){try{const key='remote.lyricCalibration.'+lyricKey;
  const saved=JSON.parse(localStorage.getItem(key)||'{}');
  localStorage.setItem(key,JSON.stringify({...saved,candidate:lyricChoice}))}catch(e){}}
function adjustLyricOffset(delta,reset=false){if(offsetConfirmed===null||!Number.isFinite(delta))return;
  lyricOffset=reset?0:Math.round(Math.max(-10,Math.min(10,lyricOffset+delta))*1000)/1000;
  offsetEdit++;offsetPending=true;offsetError='';renderLyricOffset();syncLyrics(playbackPosition(),true);
  clearTimeout(offsetTimer);offsetTimer=setTimeout(saveGlobalLyricOffset,120)}
async function saveGlobalLyricOffset(){if(offsetSaving||!offsetPending||offsetConfirmed===null)return;
  offsetSaving=true;offsetPending=false;offsetError='';renderLyricOffset();const edit=offsetEdit,value=lyricOffset;
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),8000);
  try{const response=await fetch('/settings/lyrics',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({lyric_offset:value,revision:offsetConfirmed.revision}),signal:controller.signal,keepalive:true});
    const data=await response.json();
    if(response.status===409){acceptLyricSettings(data);throw new Error('conflict')}
    if(!response.ok||!data.ok)throw new Error('save');acceptLyricSettings(data);offsetError='';
  }catch(e){offsetError=e.message==='conflict'?'其他设备已更新，请重试':'保存失败，请重试';
  }finally{clearTimeout(timeout);offsetSaving=false;
    if(edit===offsetEdit){lyricOffset=offsetConfirmed.lyric_offset;syncLyrics(playbackPosition(),true)}
    renderLyricOffset();if(offsetPending)saveGlobalLyricOffset()}}
window.addEventListener('pagehide',()=>{clearTimeout(offsetTimer);saveGlobalLyricOffset()});
function changeLyricVersion(){lyricChoice=$('lyrics-version').value;saveLyricCalibration();
  clearLyrics('切换歌词版本…');requestLyrics()}
try{lyricsVisible=localStorage.getItem('remote.showLyrics')==='true'}catch(e){}
try{translationEnabled=localStorage.getItem('remote.simplifiedTranslation')==='true'}catch(e){}
$('translation-toggle').setAttribute('aria-pressed',String(translationEnabled));

// Source, version, offset and translation settings live in one popover.
function toggleLyricSheet(open=!sheetOpen){sheetOpen=!!open&&lyricsVisible;
  $('lyrics-sheet').hidden=!sheetOpen;$('lyrics-tune').setAttribute('aria-expanded',String(sheetOpen))}
document.addEventListener('pointerdown',e=>{if(sheetOpen&&!$('lyrics-sheet').contains(e.target)&&
  !$('lyrics-tune').contains(e.target))toggleLyricSheet(false)});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&sheetOpen){toggleLyricSheet(false);$('lyrics-tune').focus()}});
function lyricStatus(text){$('lyrics-status').textContent=text;$('lyrics-scroll').dataset.empty=text;
  $('lyrics-panel').dataset.state=text.endsWith('…')?'loading':''}

function stopTranslation(){translationEpoch++;clearTimeout(translationTimer);translationTimer=0;
  if(translationRequest)translationRequest.abort();translationRequest=null}
function translationMessage(text){$('translation-status').textContent=text;
  $('translation-status').title=text;
  $('translation-status').hidden=!translationEnabled||!text}
function toggleTranslation(){translationEnabled=!translationEnabled;
  try{localStorage.setItem('remote.simplifiedTranslation',String(translationEnabled))}catch(e){}
  $('translation-toggle').setAttribute('aria-pressed',String(translationEnabled));
  stopTranslation();
  renderLyricDisplay(()=>{translationMessage('');$('translation-retry').hidden=true});
  if(translationEnabled&&rawLyrics)startTranslation()}
function renderLyricDisplay(updateUi){if(!rawLyrics){if(updateUi)updateUi();return}
  const scroll=$('lyrics-scroll');
  // Keep the first visible unit anchored while the user is browsing manually.
  const anchor=lyricNodes.find(n=>n.getBoundingClientRect().bottom>scroll.getBoundingClientRect().top);
  const anchorTop=anchor?anchor.getBoundingClientRect().top:0;
  if(updateUi)updateUi();
  lyricNodes.forEach((node,index)=>{const original=rawLyrics.display_rows?rawLyrics.display_rows[index]:
      rawLyrics.state==='synced'?rawLyrics.lines[index]:{text:(rawLyrics.plain||'').split('\\n')[index]};
    const derived=translationEnabled&&original?derivedRows.get(original.id):null;
    const text=derived?derived.text:original.text;
    node.replaceChildren();const source=document.createElement('span');source.className='lyric-original';
    source.textContent=text|| (rawLyrics.state==='synced'?'♪':'');node.appendChild(source);
    node.classList.toggle('lyric-gap',!text&&rawLyrics.state==='synced');
    if(derived&&derived.translation){const target=document.createElement('span');target.className='lyric-translation';
      target.lang='zh-Hans';target.textContent=derived.translation;node.appendChild(target)}});
  if(manualUntil>Date.now()&&anchor){const scale=scroll.getBoundingClientRect().height/scroll.offsetHeight||1;
    // Reading geometry flushes native scroll anchoring. Adjust its CURRENT
    // position so browsers with and without native anchoring behave alike.
    const delta=(anchor.getBoundingClientRect().top-anchorTop)/scale;scroll.scrollTop+=delta}
  else followLyrics(true);
  scheduleLyricsLayout()}
async function startTranslation(retry=false){if(!translationEnabled||!rawLyrics||translationRequest)return;
  stopTranslation();const epoch=translationEpoch,trackEpoch=lyricEpoch,id=rawLyrics.lyrics_id;
  if(!id){translationMessage('当前歌词暂不支持翻译');return}
  renderLyricDisplay(()=>{$('translation-retry').hidden=true;translationMessage('正在准备简体显示…')});
  async function fetchResult(start){if(!translationEnabled||epoch!==translationEpoch||trackEpoch!==lyricEpoch)return;
    const controller=new AbortController();translationRequest=controller;
    const timeout=setTimeout(()=>controller.abort(),10000);
    try{const response=await fetch('/lyrics/translation'+(start?'':'?lyrics_id='+encodeURIComponent(id)),
      start?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({lyrics_id:id,retry}),
        signal:controller.signal,cache:'no-store'}:{signal:controller.signal,cache:'no-store'});
      if(!response.ok)throw new Error('request failed');const data=await response.json();
      if(!translationEnabled||epoch!==translationEpoch||trackEpoch!==lyricEpoch||rawLyrics.lyrics_id!==id)return;
      if(!data.ok){if(data.error==='lyrics_expired'){
          renderLyricDisplay(()=>{translationMessage('歌词已过期，请重新加载歌词');$('lyrics-retry').hidden=false});return}
        throw new Error('request failed')}
      const validIds=new Set((rawLyrics.display_rows||[]).map(row=>row.id));
      if(Array.isArray(data.rows)){derivedRows=new Map(data.rows.filter(r=>validIds.has(r.id)&&typeof r.text==='string'&&
          typeof r.translation==='string').map(r=>[r.id,r]))}
      const errors={unavailable:'本地 Ollama 未运行或无法连接；本地繁简转换可用',
        model_not_found:'本地未找到 hy-mt2-translate 模型，请在 Ollama 中安装后重试',
        invalid_config:'本地翻译模型配置无效；本地繁简转换可用',rate_limited:'本地模型忙，请稍后重试缺失译文',
        timeout:'翻译请求超时，已保留成功译文',invalid_response:'翻译返回行数或内容异常，已保留成功译文',
        lyrics_expired:'歌词已过期，请重新加载歌词'};
      const hasMachine=(data.rows||[]).some(row=>row.method==='hy-mt2');
      renderLyricDisplay(()=>{translationMessage(data.state==='pending'?'正在翻译… 原歌词可继续查看':
        data.error?(errors[data.error]||'翻译暂不可用，已保留原歌词及成功译文'):
        hasMachine?'Hy-MT2 本地翻译 · 非 Apple Music 官方译文':'简体显示 · 本地转换／已有中文译文');
      $('translation-retry').hidden=!(data.state!=='pending'&&data.error&&
        !['invalid_config','lyrics_expired'].includes(data.error))});
      if(data.state==='pending')translationTimer=setTimeout(()=>{translationTimer=0;fetchResult(false)},900);
    }catch(e){if(epoch!==translationEpoch||trackEpoch!==lyricEpoch||!translationEnabled)return;
      renderLyricDisplay(()=>{translationMessage('翻译请求失败，原歌词仍可查看');$('translation-retry').hidden=false});
    }finally{clearTimeout(timeout);if(epoch===translationEpoch)translationRequest=null}}
  await fetchResult(true)}

// The artwork is the largest square inside its grid area. While lyrics are
// shown it fills that area, so the overlay keeps the exact original bounds.
function fitArt(){const art=$('art'),main=document.querySelector('#view-now main');
  if(lyricsVisible||art.parentElement!==main){if(artFit){artFit='';art.style.width=art.style.height=''}return}
  const key=[main.clientWidth,main.clientHeight,innerWidth,innerHeight,...[...main.children].slice(1).map(n=>n.offsetHeight)].join();
  if(key===artFit)return;artFit=key;art.style.width=art.style.height='';
  const size=Math.floor(Math.min(art.offsetWidth,art.offsetHeight));
  if(size>0)art.style.width=art.style.height=size+'px'}
// Position the overlay against the actual art stage, including a native wrapper.
// Never insert a sixth child in main or depend on a native message handler.
function positionLyrics(){if(layoutFrame)cancelAnimationFrame(layoutFrame);layoutFrame=0;
  fitArt();positionMotion();
  if(!lyricsVisible||view!=='now')return;
  const stage=$('remote-art-stage')||$('art'),r=stage.getBoundingClientRect();
  const panel=$('lyrics-panel');let zoom=1;
  for(let el=panel;el;el=el.parentElement)zoom*=Number(getComputedStyle(el).zoom)||1;
  panel.style.left=r.left/zoom+'px';panel.style.top=r.top/zoom+'px';
  panel.style.width=r.width/zoom+'px';panel.style.height=r.height/zoom+'px';
  panel.style.setProperty('--lyric-tail',r.height/zoom/2+'px');
  panel.style.setProperty('--lyric-head',Math.round(r.height/zoom*.2)+'px');
  followLyrics(true)}
function scheduleLyricsLayout(){if(!layoutFrame)layoutFrame=requestAnimationFrame(positionLyrics);
  // Background tabs / embedded clients can delay animation frames.
  // Re-measure after resize layout settles, even if the first frame ran early.
  clearTimeout(layoutTimer);layoutTimer=setTimeout(()=>{layoutTimer=0;positionLyrics()},160)}
function applyLyricsPreference(){document.body.classList.toggle('lyrics-visible',lyricsVisible);
  $('lyrics-panel').hidden=!lyricsVisible;$('lyric-toggle').setAttribute('aria-pressed',String(lyricsVisible));
  if(!lyricsVisible)toggleLyricSheet(false);
  positionLyrics();scheduleLyricsLayout();motionUpdate()}
function toggleLyrics(){lyricsVisible=!lyricsVisible;
  try{localStorage.setItem('remote.showLyrics',String(lyricsVisible))}catch(e){}
  applyLyricsPreference();if(lyricsVisible&&!lyricLoaded&&lyricTrack)requestLyrics()}
function clearLyrics(message){lyricEpoch++;if(lyricRequest)lyricRequest.abort();lyricRequest=null;
  stopTranslation();rawLyrics=null;derivedRows.clear();translationMessage('');$('translation-retry').hidden=true;
  lyricLoaded=false;lyricLines=[];lyricNodes=[];lyricActive=-1;manualUntil=0;flipUntil=0;
  $('lyrics-panel').classList.remove('browsing');
  $('lyrics-scroll').replaceChildren();$('lyrics-scroll').scrollTop=0;
  lyricStatus(message);$('lyrics-follow').hidden=true;$('lyrics-retry').hidden=true;$('lyrics-tune').classList.remove('attention');
  $('lyrics-source-label').textContent='LDDC 多源 · 第三方歌词'}
function beginTrackChange(){playbackEpoch++;pendingTrackKey=lyricKey;pendingTrackUntil=Date.now()+6000;
  clearLyrics('切歌中…')}
// Every status (or its absence) also refreshes the current song's catalog match.
function updateLyricTrack(s){updateLyricTrackState(s);catalogTrack(s)}
function updateLyricTrackState(s){const track=s&&s.ok?{
    title:s.title||'',artist:s.lyric_artist||s.artist||'',album:s.album||'',duration:Math.round(s.dur||0)}:null;
  const key=track?JSON.stringify(track):'';
  if(pendingTrackKey&&key===pendingTrackKey&&Date.now()<pendingTrackUntil)return;
  pendingTrackKey='';
  if(key===lyricKey)return;lyricKey=key;lyricTrack=track;
  clearLyrics(track?'加载中…':'等待歌曲信息');loadLyricCalibration();
  if(track&&lyricsVisible)requestLyrics()}
async function requestLyrics(){if(!lyricTrack||lyricRequest)return;
  if(pendingTrackKey&&Date.now()<pendingTrackUntil)return;
  const epoch=++lyricEpoch,key=lyricKey,controller=new AbortController();lyricRequest=controller;
  lyricStatus('加载中…');$('lyrics-retry').hidden=true;
  const timeout=setTimeout(()=>controller.abort(),21000);
  try{const params=new URLSearchParams(lyricTrack);
    params.set('candidate',lyricChoice);
    const response=await fetch('/lyrics?'+params,{signal:controller.signal,cache:'no-store'});
    if(!response.ok)throw new Error('HTTP '+response.status);const data=await response.json();
    if(epoch!==lyricEpoch||key!==lyricKey)return;
    if(data.error==='candidate_expired'&&lyricChoice!=='auto'){
      lyricChoice='auto';saveLyricCalibration();setTimeout(requestLyrics,0);return}
    if(data.state==='error'||!data.ok)throw new Error(data.error||'request failed');
    lyricLoaded=true;stopTranslation();derivedRows.clear();rawLyrics=data;
    $('lyrics-source-label').textContent=(data.source||'LDDC 多源')+' · 第三方歌词';
    const versions=$('lyrics-version');versions.replaceChildren(new Option('自动匹配来源／版本','auto'));
    (data.candidates||[]).forEach(c=>{const r=c.record||{};
      versions.add(new Option((c.approximate?'近似 · ':'')+c.source+' · '+(r.trackName||'')+' · '+(r.albumName||'未知专辑')+' · '+fmt(r.duration||0),c.id))});
    versions.value=lyricChoice;
    const messages={not_found:'暂无匹配歌词',instrumental:'纯音乐',synced:'逐行同步',plain:'普通歌词 · 无时间戳'};
    const approximate=data.match_quality==='approximate';
    lyricStatus((messages[data.state]||'暂无匹配歌词')+(approximate?' · 近似匹配':'')+(data.match_requires_selection?' · 可选择其他版本':''));
    // A dot on 调整 points to the source/version picker when the match is ambiguous or approximate.
    $('lyrics-tune').classList.toggle('attention',!!data.match_requires_selection||approximate);
    lyricLines=[];lyricNodes=[];lyricActive=-1;flipUntil=0;$('lyrics-scroll').replaceChildren();
    if(data.state==='synced'&&Array.isArray(data.lines)){
      lyricLines=data.lines;const fragment=document.createDocumentFragment();
      lyricNodes=lyricLines.map(line=>{const p=document.createElement('p');p.className='lyric-line';
        p.textContent=line.text||'♪';if(data.display_rows&&data.display_rows[fragment.childNodes.length])
          p.dataset.lineId=data.display_rows[fragment.childNodes.length].id;
        fragment.appendChild(p);return p});
      $('lyrics-scroll').replaceChildren(fragment);syncLyrics(playbackPosition(),true);
    }else if(data.state==='plain'){
      const p=document.createElement('div');p.className='lyrics-plain';
      lyricNodes=(data.display_rows|| (data.plain||'').split('\\n').map(text=>({text}))).map(row=>{
        const unit=document.createElement('p');unit.className='lyric-line';unit.textContent=row.text;
        if(row.id)unit.dataset.lineId=row.id;p.appendChild(unit);return unit});
      $('lyrics-scroll').replaceChildren(p)}
    if(['synced','plain'].includes(data.state)){renderLyricDisplay();if(translationEnabled)startTranslation()}
  }catch(e){if(epoch!==lyricEpoch||key!==lyricKey)return;
    lyricLoaded=true;lyricStatus('歌词请求失败，请稍后重试');$('lyrics-retry').hidden=false;
  }finally{clearTimeout(timeout);if(epoch===lyricEpoch)lyricRequest=null}}
function playbackPosition(){return Math.max(0,Math.min(scrubbing?+$('pr').value:
  pos+(playing?(performance.now()-posTs)/1000:0),dur||0))}
function syncLyrics(p,force=false){p-=lyricOffset;let lo=0,hi=lyricLines.length,animate=false;
  while(lo<hi){const mid=(lo+hi)>>1;if(lyricLines[mid].time<=p)lo=mid+1;else hi=mid}
  const active=lo-1;if(active!==lyricActive){const previous=lyricActive;
    if(lyricNodes[lyricActive]){lyricNodes[lyricActive].classList.remove('current');lyricNodes[lyricActive].removeAttribute('aria-current')}
    lyricActive=active;if(lyricNodes[active]){lyricNodes[active].classList.add('current');lyricNodes[active].setAttribute('aria-current','true')}
    markLyricDistance(previous,active);animate=previous>=0&&active>=0&&!scrubbing;force=true}
  if(manualUntil&&Date.now()>=manualUntil){manualUntil=0;$('lyrics-follow').hidden=true;
    $('lyrics-panel').classList.remove('browsing');force=true;animate=true}
  if(force)followLyrics(false,animate)}
// --d (distance from the current line) drives the dimming and blur depth.
function markLyricDistance(previous,active){if(!lyricLines.length)return;
  const from=Math.max(0,Math.min(previous,active)-7),to=Math.min(lyricNodes.length-1,Math.max(previous,active)+7);
  for(let i=from;i<=to;i++)lyricNodes[i].style.setProperty('--d',String(Math.min(6,Math.abs(i-active))))}
// Layout position inside the scroller; unaffected by the visual line transforms.
function lineTop(el,scroll){let top=0,node=el;
  while(node&&node!==scroll){top+=node.offsetTop;node=node.offsetParent}
  return node===scroll?top:null}
function followLyrics(force=false,animate=false){if(!lyricsVisible||view!=='now'||manualUntil>Date.now())return;
  const el=lyricNodes[lyricActive],scroll=$('lyrics-scroll');if(!el)return;
  let top=lineTop(el,scroll);
  if(top===null){const scale=scroll.getBoundingClientRect().height/scroll.offsetHeight||1;
    top=(el.getBoundingClientRect().top-scroll.getBoundingClientRect().top)/scale+scroll.scrollTop}
  const target=Math.max(0,top-(scroll.clientHeight-el.offsetHeight)/2);
  if(!force&&Math.abs(scroll.scrollTop-target)<=2)return;
  const before=scroll.scrollTop;scroll.scrollTop=target;const delta=scroll.scrollTop-before;
  if(!delta)return;
  if(animate&&motionOK())flipLyrics(delta);else settleLyrics()}
// FLIP: scrollTop is already final; lines start at their old visual place and
// spring into position one after another, like Apple Music's lyric wave.
function flipLyrics(delta){const scroll=$('lyrics-scroll'),height=scroll.clientHeight,top=scroll.scrollTop;
  const jump=Math.abs(delta)>height*1.25,shift=jump?Math.sign(delta)*Math.min(140,height*.3):delta;
  const from=(jump?top:Math.min(top,top-delta))-height*.1,to=(jump?top:Math.max(top,top-delta))+height*1.1;
  const moving=[];
  lyricNodes.forEach((node,index)=>{const y=lineTop(node,scroll);if(y===null||y+node.offsetHeight<from||y>to)return;
    moving.push([node,index,parseFloat(String(getComputedStyle(node).translate).split(' ')[1])||0])});
  moving.forEach(([node,index,current])=>{node.style.transition='none';node.style.translate='0 '+(current+shift)+'px';
    if(jump)node.style.opacity='0'});
  void scroll.offsetHeight;
  moving.forEach(([node,index])=>{const order=delta>0?index-lyricActive:lyricActive-index;
    node.style.setProperty('--delay',Math.max(0,Math.min(8,order+2))*34+'ms');
    node.style.transition='';node.style.translate='';node.style.opacity=''});
  flipUntil=performance.now()+1300}
function settleLyrics(){if(performance.now()>=flipUntil)return;flipUntil=0;
  lyricNodes.forEach(node=>{node.style.transition='none';node.style.opacity=''});void $('lyrics-scroll').offsetHeight;
  lyricNodes.forEach(node=>{node.style.transition=''})}
function resumeLyrics(){manualUntil=0;$('lyrics-follow').hidden=true;$('lyrics-panel').classList.remove('browsing');
  followLyrics(true,true)}
function pauseLyricsFollow(){if(!lyricLines.length)return;manualUntil=Date.now()+8000;$('lyrics-follow').hidden=false;
  $('lyrics-panel').classList.add('browsing');settleLyrics()}
['wheel','touchstart','pointerdown'].forEach(event=>$('lyrics-scroll').addEventListener(event,pauseLyricsFollow,{passive:true}));
$('lyrics-scroll').addEventListener('keydown',e=>{if(['ArrowUp','ArrowDown','PageUp','PageDown','Home','End',' '].includes(e.key))pauseLyricsFollow()});
$('lyrics-scroll').addEventListener('scroll',()=>{if(manualUntil>Date.now())manualUntil=Date.now()+8000},{passive:true});
window.addEventListener('resize',scheduleLyricsLayout);window.addEventListener('orientationchange',scheduleLyricsLayout);
if(window.visualViewport)window.visualViewport.addEventListener('resize',scheduleLyricsLayout);
$('view-now').addEventListener('transitionend',e=>{if(e.target===$('view-now'))scheduleLyricsLayout()});
if(window.ResizeObserver){const observer=new ResizeObserver(scheduleLyricsLayout);
  observer.observe($('art'));observer.observe(document.querySelector('#view-now main'));
  const watchStage=()=>{const stage=$('remote-art-stage');if(stage)observer.observe(stage);scheduleLyricsLayout()};
  new MutationObserver(watchStage).observe(document.querySelector('#view-now main'),{childList:true,subtree:true})}
$('art').addEventListener('load',scheduleLyricsLayout);
applyLyricsPreference();

// Artwork backdrop. WebGL (Apple Music web style): five rotating artwork
// layers, two of them orbiting, are swirled, blurred several times at low
// resolution and saturated, at 15 fps. Without WebGL the blurred copies drift
// on a 64px 2D canvas. Both pause with playback and while hidden, and
// cross-fade over 1.3 s on track changes. The compositor scales either up.
const BG={canvas:$('bg-canvas'),ctx:null,gl:null,from:null,to:null,mix:1,mixStart:0,frame:0,drawn:0,clock:0,tickAt:0};
const BLOBS=[{s:1.3,a:.85,w:.09,p:0,rx:.16,ry:.12,fx:.13,fy:.11},{s:1.05,a:.75,w:-.12,p:2.1,rx:.26,ry:.2,fx:.1,fy:.15},
  {s:.9,a:.7,w:.15,p:4.2,rx:.24,ry:.28,fx:.17,fy:.12},{s:.75,a:.65,w:-.2,p:1.3,rx:.3,ry:.24,fx:.12,fy:.19}];
// s: size relative to the long viewport edge (0 = cover the diagonal); soft: edge fade.
const FLUID_LAYERS=[{s:0,a:1,w:.03,p:0,soft:0},{s:1.25,a:.8,w:.05,p:.6,soft:.35},{s:.8,a:.75,w:-.07,p:1.2,ox:.12,oy:-.08,soft:.5},
  {s:.5,a:.7,w:.13,p:2.4,orbit:.3,speed:.11,soft:.6},{s:.25,a:.65,w:-.2,p:4.1,orbit:.42,speed:-.17,soft:.7}];
const FLUID_VS='attribute vec2 p;uniform vec2 c;uniform vec2 k;uniform float r;varying vec2 v;'+
  'void main(){float s=sin(r),o=cos(r);gl_Position=vec4(c+vec2(p.x*o-p.y*s,p.x*s+p.y*o)*k,0.,1.);v=p*.5+.5;}';
const FLUID_SPRITE='precision mediump float;uniform sampler2D t;uniform float a;uniform float m;varying vec2 v;'+
  'void main(){float d=length(v-.5)*2.;float f=m>0.?1.-smoothstep(1.-m,1.,d):1.;gl_FragColor=vec4(texture2D(t,v).rgb,a*f);}';
const FLUID_PASS='attribute vec2 p;varying vec2 v;void main(){gl_Position=vec4(p,0.,1.);v=p*.5+.5;}';
const FLUID_SWIRL='precision mediump float;uniform sampler2D t;uniform float time;uniform float aspect;uniform float amount;varying vec2 v;'+
  'void main(){vec2 c=v-.5;c.x*=aspect;float d=length(c);float a=amount*(1.-smoothstep(0.,.95,d))*sin(time*.21+d*2.6);'+
  'float s=sin(a),o=cos(a);c=vec2(c.x*o-c.y*s,c.x*s+c.y*o);c.x/=aspect;gl_FragColor=texture2D(t,c+.5);}';
const FLUID_BLUR='precision mediump float;uniform sampler2D t;uniform vec2 px;varying vec2 v;'+
  'void main(){gl_FragColor=texture2D(t,v)*.227+(texture2D(t,v+px*1.385)+texture2D(t,v-px*1.385))*.316'+
  '+(texture2D(t,v+px*3.231)+texture2D(t,v-px*3.231))*.0703;}';
const FLUID_FINAL='precision mediump float;uniform sampler2D t;uniform float sat;uniform float veil;varying vec2 v;'+
  'void main(){vec3 c=texture2D(t,v).rgb;c=mix(vec3(dot(c,vec3(.2126,.7152,.0722))),c,sat)*(1.-veil);gl_FragColor=vec4(clamp(c,0.,1.),1.);}';
function fluidProgram(gl,vs,fs){const p=gl.createProgram();
  [[gl.VERTEX_SHADER,vs],[gl.FRAGMENT_SHADER,fs]].forEach(([type,source])=>{const s=gl.createShader(type);gl.shaderSource(s,source);
    gl.compileShader(s);if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw Error(gl.getShaderInfoLog(s));gl.attachShader(p,s)});
  gl.bindAttribLocation(p,0,'p');gl.linkProgram(p);if(!gl.getProgramParameter(p,gl.LINK_STATUS))throw Error(gl.getProgramInfoLog(p));
  const u={};for(let i=0;i<gl.getProgramParameter(p,gl.ACTIVE_UNIFORMS);i++){const n=gl.getActiveUniform(p,i).name;u[n]=gl.getUniformLocation(p,n)}
  return {p,u}}
function fluidTexture(gl,source,w,h){const tex=gl.createTexture();gl.bindTexture(gl.TEXTURE_2D,tex);
  if(source)gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,gl.RGBA,gl.UNSIGNED_BYTE,source);
  else gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,w,h,0,gl.RGBA,gl.UNSIGNED_BYTE,null);
  [gl.TEXTURE_MIN_FILTER,gl.TEXTURE_MAG_FILTER].forEach(k=>gl.texParameteri(gl.TEXTURE_2D,k,gl.LINEAR));
  [gl.TEXTURE_WRAP_S,gl.TEXTURE_WRAP_T].forEach(k=>gl.texParameteri(gl.TEXTURE_2D,k,gl.CLAMP_TO_EDGE));return tex}
function fluidTarget(gl,w,h){const tex=fluidTexture(gl,null,w,h),fb=gl.createFramebuffer();gl.bindFramebuffer(gl.FRAMEBUFFER,fb);
  gl.framebufferTexture2D(gl.FRAMEBUFFER,gl.COLOR_ATTACHMENT0,gl.TEXTURE_2D,tex,0);return {tex,fb,w,h}}
// Software-only WebGL (GPU disabled or blocklisted, e.g. WARP) counts as no
// WebGL: the 2D canvas is far cheaper there, and in headless browsers a WARP
// canvas can stall frame production for the rest of the page.
function initFluid(canvas){try{
  const gl=canvas.getContext('webgl',{alpha:false,antialias:false,depth:false,stencil:false,powerPreference:'low-power',
    failIfMajorPerformanceCaveat:true});
  if(!gl)return null;
  const quad=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,quad);
  gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,1,-1,-1,1,1,1]),gl.STATIC_DRAW);
  gl.enableVertexAttribArray(0);gl.vertexAttribPointer(0,2,gl.FLOAT,false,0,0);
  return {gl,sprite:fluidProgram(gl,FLUID_VS,FLUID_SPRITE),swirl:fluidProgram(gl,FLUID_PASS,FLUID_SWIRL),
    blur:fluidProgram(gl,FLUID_PASS,FLUID_BLUR),final:fluidProgram(gl,FLUID_PASS,FLUID_FINAL),targets:null}}catch(e){return null}}
function fluidPass(program,source,target,values){const gl=BG.gl.gl;gl.useProgram(program.p);
  gl.bindFramebuffer(gl.FRAMEBUFFER,target?target.fb:null);
  gl.viewport(0,0,target?target.w:gl.drawingBufferWidth,target?target.h:gl.drawingBufferHeight);gl.bindTexture(gl.TEXTURE_2D,source.tex);
  for(const k in values){const u=program.u[k],v=values[k];if(u)Array.isArray(v)?gl.uniform2f(u,v[0],v[1]):gl.uniform1f(u,v)}
  gl.drawArrays(gl.TRIANGLE_STRIP,0,4)}
function drawFluid(){const f=BG.gl,gl=f.gl,W=gl.drawingBufferWidth,H=gl.drawingBufferHeight;if(!W||!H)return;
  if(!f.targets||f.targets.w!==W||f.targets.h!==H){
    if(f.targets)['scene','a','b'].forEach(k=>{gl.deleteTexture(f.targets[k].tex);gl.deleteFramebuffer(f.targets[k].fb)});
    const w=Math.max(8,W>>1),h=Math.max(8,H>>1);f.targets={w:W,h:H,scene:fluidTarget(gl,W,H),a:fluidTarget(gl,w,h),b:fluidTarget(gl,w,h)}}
  const T=f.targets,t=BG.clock,ref=Math.max(W,H),base=(BG.to||BG.from||{avg:[60,56,70]}).avg;
  gl.bindFramebuffer(gl.FRAMEBUFFER,T.scene.fb);gl.viewport(0,0,W,H);
  gl.clearColor(base[0]*.5/255,base[1]*.5/255,base[2]*.5/255,1);gl.clear(gl.COLOR_BUFFER_BIT);
  gl.enable(gl.BLEND);gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA);gl.useProgram(f.sprite.p);
  [[BG.from,1-BG.mix],[BG.to,BG.mix]].forEach(([source,weight])=>{if(!source||!source.tex||weight<.004)return;
    gl.bindTexture(gl.TEXTURE_2D,source.tex);
    FLUID_LAYERS.forEach(layer=>{const size=layer.s?layer.s*ref:Math.hypot(W,H)*1.12;
      let x=(layer.ox||0)*ref,y=(layer.oy||0)*ref;
      if(layer.orbit){const a=t*layer.speed+layer.p;x=Math.cos(a)*layer.orbit*ref;y=Math.sin(a*1.3)*layer.orbit*ref*.8}
      gl.uniform2f(f.sprite.u.c,x*2/W,y*2/H);gl.uniform2f(f.sprite.u.k,size/W,size/H);gl.uniform1f(f.sprite.u.r,t*layer.w+layer.p);
      gl.uniform1f(f.sprite.u.a,weight*layer.a);gl.uniform1f(f.sprite.u.m,layer.soft);gl.drawArrays(gl.TRIANGLE_STRIP,0,4)})});
  gl.disable(gl.BLEND);
  fluidPass(f.swirl,T.scene,T.a,{time:t,aspect:W/H,amount:1.1});
  fluidPass(f.blur,T.a,T.b,{px:[1/T.a.w,0]});fluidPass(f.blur,T.b,T.a,{px:[0,1/T.a.h]});
  fluidPass(f.blur,T.a,T.b,{px:[2/T.a.w,0]});fluidPass(f.blur,T.b,T.a,{px:[0,2/T.a.h]});
  // Brighter artwork gets a stronger veil so white text keeps its contrast.
  const lum=(BG.to||{lum:.2}).lum;fluidPass(f.final,T.a,null,{sat:1.6,veil:Math.min(.66,Math.max(.24,.1+lum*.7))})}
function fluidSource(img){const gl=BG.gl&&BG.gl.gl;if(!gl)return null;try{
  const c=document.createElement('canvas');c.width=c.height=96;const x=c.getContext('2d');x.imageSmoothingQuality='high';
  x.drawImage(img,0,0,96,96);return fluidTexture(gl,c)}catch(e){return null}}
function dropSource(source){if(source&&source.tex&&BG.gl){BG.gl.gl.deleteTexture(source.tex);source.tex=null}}
function useBackdrop2D(){BG.gl=null;$('bg-gl').hidden=true;BG.canvas.hidden=false;
  if(!BG.ctx)try{BG.ctx=BG.canvas.getContext('2d',{alpha:false})}catch(e){}
  BG.canvas.width=BG.canvas.height=0;fitBackdrop();drawBackdrop()}
BG.gl=initFluid($('bg-gl'));
if(BG.gl){$('bg-gl').hidden=false;BG.canvas.hidden=true;
  $('bg-gl').addEventListener('webglcontextlost',e=>{e.preventDefault();useBackdrop2D()})}
else try{BG.ctx=BG.canvas.getContext('2d',{alpha:false})}catch(e){}
// Match the viewport's aspect ratio so drifting blobs stay round when scaled up.
function fitBackdrop(){const w=innerWidth||1,h=innerHeight||1,canvas=BG.gl?$('bg-gl'):BG.canvas,scale=(BG.gl?160:72)/Math.max(w,h);
  const width=Math.max(16,Math.round(w*scale)),height=Math.max(16,Math.round(h*scale));
  if(!(BG.ctx||BG.gl)||canvas.width===width&&canvas.height===height)return;
  canvas.width=width;canvas.height=height;
  if(!BG.gl){BG.ctx.imageSmoothingEnabled=true;try{BG.ctx.imageSmoothingQuality='high'}catch(e){}}drawBackdrop()}
// Test and diagnostics hook: the pixels currently drawn by either renderer.
function backdropSample(){if(BG.gl){const gl=BG.gl.gl;drawFluid();const w=gl.drawingBufferWidth,h=gl.drawingBufferHeight,data=new Uint8Array(w*h*4);
    gl.readPixels(0,0,w,h,gl.RGBA,gl.UNSIGNED_BYTE,data);return {mode:'webgl',width:w,height:h,data}}
  const c=BG.canvas;return {mode:'2d',width:c.width,height:c.height,data:BG.ctx?BG.ctx.getImageData(0,0,c.width,c.height).data:new Uint8Array(4)}}
function backdropSource(img){try{
  const full=document.createElement('canvas');full.width=full.height=32;const fx=full.getContext('2d');
  fx.imageSmoothingQuality='high';fx.drawImage(img,0,0,32,32);let avg=[60,56,70];
  try{const data=fx.getImageData(0,0,32,32),d=data.data;let r=0,g=0,b=0;
    for(let i=0;i<d.length;i+=4){const l=d[i]*.3+d[i+1]*.59+d[i+2]*.11;
      for(let k=0;k<3;k++)d[i+k]=Math.max(0,Math.min(255,l+(d[i+k]-l)*1.5));r+=d[i];g+=d[i+1];b+=d[i+2]}
    fx.putImageData(data,0,0);avg=[r,g,b].map(v=>v/(d.length/4))}catch(e){}
  const blob=document.createElement('canvas');blob.width=blob.height=32;const bx=blob.getContext('2d');bx.drawImage(full,0,0);
  const fade=bx.createRadialGradient(16,16,0,16,16,16);fade.addColorStop(0,'#000');fade.addColorStop(.5,'rgba(0,0,0,.7)');
  fade.addColorStop(.8,'rgba(0,0,0,.25)');fade.addColorStop(1,'rgba(0,0,0,0)');
  bx.globalCompositeOperation='destination-in';bx.fillStyle=fade;bx.fillRect(0,0,32,32);
  return {full,blob,avg,lum:(avg[0]*.2126+avg[1]*.7152+avg[2]*.0722)/255}}catch(e){return null}}
function paintRotated(ctx,image,x,y,angle,size){ctx.save();ctx.translate(x,y);ctx.rotate(angle);
  ctx.drawImage(image,-size/2,-size/2,size,size);ctx.restore()}
function drawBackdrop(){if(BG.gl){try{drawFluid()}catch(e){useBackdrop2D()}return}
  const ctx=BG.ctx;if(!ctx)return;
  const width=BG.canvas.width,height=BG.canvas.height,size=Math.sqrt(width*height),t=BG.clock;
  const base=(BG.to||BG.from||{avg:[60,56,70]}).avg;
  ctx.globalAlpha=1;ctx.fillStyle='rgb('+base.map(v=>Math.round(v*.5)).join(',')+')';ctx.fillRect(0,0,width,height);
  [[BG.from,1-BG.mix],[BG.to,BG.mix]].forEach(([source,weight])=>{if(!source||weight<.004)return;
    ctx.globalAlpha=weight;paintRotated(ctx,source.full,width/2,height/2,t*.03,Math.hypot(width,height)*1.12);
    BLOBS.forEach(blob=>{ctx.globalAlpha=weight*blob.a;paintRotated(ctx,source.blob,
      width*(.5+blob.rx*Math.sin(t*blob.fx+blob.p)),height*(.5+blob.ry*Math.cos(t*blob.fy+blob.p*1.3)),t*blob.w+blob.p,size*blob.s)})});
  const lum=(BG.to||{lum:.2}).lum;ctx.globalAlpha=1;
  // Brighter artwork gets a stronger veil so white text keeps its contrast.
  ctx.fillStyle='rgba(0,0,0,'+Math.min(.66,Math.max(.24,.1+lum*.7)).toFixed(3)+')';ctx.fillRect(0,0,width,height)}
// A full-screen motion video covers the backdrop; stop drawing underneath it.
function backdropActive(){return !document.hidden&&!document.body.classList.contains('motion-tall')&&
  (BG.mix<1||playing&&!reducedMotion.matches)}
function backdropFrame(now){BG.frame=0;const step=Math.min(.1,Math.max(0,(now-BG.tickAt)/1000));BG.tickAt=now;
  if(playing&&motionOK())BG.clock+=step;
  let finished=false;
  if(BG.mix<1){BG.mix=reducedMotion.matches?1:Math.min(1,(now-BG.mixStart)/1300);if(BG.mix>=1){dropSource(BG.from);BG.from=null;finished=true}}
  if(finished||now-BG.drawn>=66){BG.drawn=now;drawBackdrop()}
  if(backdropActive())BG.frame=requestAnimationFrame(backdropFrame)}
function bgKick(){if(!(BG.ctx||BG.gl)||BG.frame)return;BG.tickAt=performance.now();
  if(backdropActive())BG.frame=requestAnimationFrame(backdropFrame);
  else{if(BG.mix<1){BG.mix=1;dropSource(BG.from);BG.from=null}drawBackdrop()}}
function setBackdrop(img){const source=img?backdropSource(img):null;
  if(source&&BG.gl)source.tex=fluidSource(img);
  if(BG.mix>=.5){dropSource(BG.from);BG.from=BG.to}else dropSource(BG.to);
  BG.to=source;BG.mix=0;BG.mixStart=performance.now();updateThemeColor();bgKick()}
// theme-color follows the album's key color when the catalog knows the song.
function updateThemeColor(){const hex=nowCatalog.album&&nowCatalog.album.artwork&&nowCatalog.album.artwork.bg;
  const rgb=/^[0-9a-f]{6}$/i.test(hex||'')?[0,2,4].map(i=>parseInt(hex.slice(i,i+2),16)):BG.to&&BG.to.avg;
  if(rgb)document.querySelector('meta[name=theme-color]').content='rgb('+rgb.map(v=>Math.round(v*.4)).join(',')+')'}
let artworkJob=null;
function cancelArtwork(){if(artworkJob){artworkJob.timers.forEach(clearTimeout);
    artworkJob.images.forEach(img=>{img.onload=img.onerror=null;img.src=''})}
  artworkJob=null;++artToken}
function loadArtwork(key){cancelArtwork();showArtwork('',null,false);if(!key)return;
  const job={key,timers:[],images:new Set(),shown:false};artworkJob=job;
  const request=()=>{if(artworkJob!==job||last!==key)return;
    const token=++artToken,img=new Image(),url='/artwork?track='+encodeURIComponent(key)+'&request='+token+'&t='+Date.now();
    job.images.add(img);
    img.onload=()=>{job.images.delete(img);if(artworkJob!==job||last!==key||token!==artToken)return;
      showArtwork(url,img,!job.shown);job.shown=true};
    img.onerror=()=>{job.images.delete(img);if(artworkJob===job&&last===key&&token===artToken&&!job.shown)showArtwork('',null,false)};
    img.src=url};
  request();job.timers=[1000,3000,8000].map(ms=>setTimeout(request,ms))}
function showArtwork(url,img,animate=true){const art=$('art');art.classList.toggle('art-missing',!img);
  if(img)art.src=url;else art.removeAttribute('src');
  if(animate){art.classList.remove('art-swap');void art.offsetWidth;art.classList.add('art-swap')}setBackdrop(img)}
if(reducedMotion.addEventListener)reducedMotion.addEventListener('change',()=>{bgKick();motionUpdate()});
fitBackdrop();window.addEventListener('resize',fitBackdrop);

// Views: now playing, queue, catalog search/album/artist. Retired library
// names (pls, trs, src) fall back to now playing.
const VIEWS={now:'view-now',que:'view-que',search:'view-src',album:'view-alb',artist:'view-art'};
function show(v){if(!Object.prototype.hasOwnProperty.call(VIEWS,v))v='now';view=v;
  ['view-now','view-pls','view-trs','view-src','view-que','view-alb','view-art'].forEach(id=>$(id).classList.toggle('off',id!==VIEWS[v]));
  if(v==='now'){navStack=[];navCurrent=null;scheduleLyricsLayout()}else toggleLyricSheet(false);
  $('hbtn').hidden=v==='now';$('hbtn').innerHTML=BACKICON;
  $('hright').style.visibility=v==='now'?'visible':'hidden';
  $('htitle').textContent=v==='que'?'待播队列':v==='search'?'搜索':'';
  document.querySelectorAll('.page-motion').forEach(video=>{if(v==='album'&&video.isConnected)playPageMotion(video);else video.pause()});
  motionUpdate()}

// ------------------------------------------------------------------
// Catalog: search, album and artist pages, current-song match, motion art.
// Catalog text and URLs are untrusted: text nodes only, https images only.
// ------------------------------------------------------------------
const PLAYGLYPH='<svg viewBox="0 0 24 24"><path d="M8.2 4.6c-.8-.5-1.7.1-1.7 1v12.8c0 .9.9 1.5 1.7 1l10.4-6.4c.7-.5.7-1.5 0-2z"/></svg>';
const EXTICON='<svg viewBox="0 0 24 24"><path d="M14 4h6v6h-2V7.4l-7.3 7.3-1.4-1.4L16.6 6H14zM5 6h6v2H6v10h10v-5h2v7H4V6z"/></svg>';
function el(tag,props,...kids){const node=document.createElement(tag);
  for(const k in props||{}){const v=props[k];if(v==null||v===false)continue;
    if(k==='class')node.className=v;else if(k==='text')node.textContent=v;else if(k==='icon')node.innerHTML=v;
    else if(k.startsWith('on'))node.addEventListener(k.slice(2),v);else node.setAttribute(k,v===true?'':String(v))}
  kids.flat().forEach(kid=>{if(kid!=null&&kid!==false)node.append(kid)});return node}
function artUrl(art,w,h=w){const url=art&&typeof art.url==='string'?art.url:'';if(!url.startsWith('https://'))return '';
  const dpr=Math.min(3,Math.max(1,window.devicePixelRatio||1));return url.replace('{w}',Math.round(w*dpr)).replace('{h}',Math.round(h*dpr))}
function artImg(art,size,cls){const src=artUrl(art,size);
  return src?el('img',{class:cls,src,alt:'',loading:'lazy',decoding:'async',width:size,height:size}):el('div',{class:cls})}
function hexRgb(hex){return /^[0-9a-f]{6}$/i.test(hex||'')?[0,2,4].map(i=>parseInt(hex.slice(i,i+2),16)):null}
function setTint(id,hex){const rgb=hexRgb(hex)||[64,64,70],node=$(id);
  node.style.setProperty('--tint','rgb('+rgb.map(v=>Math.round(v*.42)).join(',')+')');node.style.setProperty('--tint-hi','rgb('+rgb.join(',')+')')}
function explicitBadge(){return el('span',{class:'explicit',text:'E',title:'儿童不宜','aria-label':'儿童不宜'})}
function norm(s){return String(s||'').normalize('NFKC').toLowerCase().replace(/[^\\p{L}\\p{N}]+/gu,' ').trim()}
function expandable(text,lines){const box=el('div',{class:'expand',style:'--lines:'+lines}),body=el('div',{class:'body',text});
  const more=el('button',{type:'button','aria-expanded':'false',text:'更多',onclick:()=>{const open=!box.classList.contains('open');
    box.classList.toggle('open',open);more.textContent=open?'收起':'更多';more.setAttribute('aria-expanded',String(open))}});
  box.append(body,more);requestAnimationFrame(()=>{if(body.scrollHeight<=body.clientHeight+2&&!box.classList.contains('open'))more.hidden=true});return box}
async function loadCatalog(kind,id,signal){const key=kind+':'+id;if(catalogCache.has(key))return catalogCache.get(key);
  const response=await timedFetch('/catalog/'+kind+'/'+encodeURIComponent(id),{signal},20000);const data=await response.json().catch(()=>({}));
  if(!response.ok||!data.ok){const error=Error(data.err||'曲库暂时不可用，请重试');error.code=data.error;error.retry=data.retry_after;throw error}
  catalogCache.set(key,data);if(catalogCache.size>40)catalogCache.delete(catalogCache.keys().next().value);return data}
function catalogMessage(e){return e.name==='AbortError'?'曲库响应超时，请重试':
  e.code==='rate_limited'?'请求过于频繁'+(e.retry?'，请 '+e.retry+' 秒后再试':'，请稍后再试'):(e.message||'无法连接遥控服务')}

// Navigation: a back stack of catalog pages; #hbtn and Escape go back.
function go(state){if(navCurrent&&view!=='now'&&view!=='que'){navCurrent.scroll=pageScroller().scrollTop;navStack.push(navCurrent)}
  navCurrent=state;enterState(state,false)}
function goBack(){if(view==='now')return;const previous=navStack.pop();
  if(previous){navCurrent=previous;enterState(previous,true)}else show('now')}
function enterState(state,restoring){if(state.v==='search')enterSearch(state,restoring);
  else if(state.v==='album')enterAlbum(state,restoring);else if(state.v==='artist')enterArtist(state,restoring)}
function pageScroller(){return $({search:'src-wrap',album:'alb-body',artist:'art-body'}[view]||'src-wrap')}
$('hbtn').onclick=goBack;
document.addEventListener('keydown',e=>{if(e.key!=='Escape'||e.defaultPrevented||sheetOpen)return;
  if(!$('artist-choice').hidden){e.preventDefault();closeArtistChoice();return}
  if(view==='now'||(e.target===$('sq')&&$('sq').value))return;e.preventDefault();goBack()});

// Search: Enter or a 500 ms pause; recent searches when empty.
function openSearch(term,kind){go({v:'search',term:term||'',kind:kind||''})}
function enterSearch(state,restoring){show('search');if(state.kind)setSearchKind(state.kind,false);
  if(!restoring&&state.term){$('sq').value=state.term;runSearch(true)}else if(!$('sq').value.trim())renderRecent();
  if(restoring)$('src-wrap').scrollTop=state.scroll||0;else setTimeout(()=>{try{$('sq').focus({preventScroll:true})}catch(e){}},60)}
function setSearchKind(kind,run=true){searchKind=kind;
  $('src-kinds').querySelectorAll('button').forEach(b=>{const on=b.dataset.kind===kind;b.setAttribute('aria-selected',String(on));b.tabIndex=on?0:-1});
  if(run&&$('sq').value.trim())runSearch(false)}
$('src-kinds').addEventListener('click',e=>{const b=e.target.closest('button');if(b)setSearchKind(b.dataset.kind)});
$('src-kinds').addEventListener('keydown',e=>{if(e.key!=='ArrowLeft'&&e.key!=='ArrowRight')return;
  const tabs=[...$('src-kinds').querySelectorAll('button')],i=tabs.findIndex(b=>b.dataset.kind===searchKind);
  const next=tabs[(i+(e.key==='ArrowRight'?1:tabs.length-1))%tabs.length];e.preventDefault();setSearchKind(next.dataset.kind);next.focus()});
$('sq').addEventListener('input',()=>{clearTimeout(searchTimer);if(!$('sq').value.trim()){searchSeq++;if(searchController)searchController.abort();renderRecent();return}
  searchTimer=setTimeout(()=>runSearch(false),500)});
function recentSearches(){try{const list=JSON.parse(localStorage.getItem('remote.recentSearches')||'[]');
  return Array.isArray(list)?list.filter(x=>typeof x==='string'&&x).slice(0,8):[]}catch(e){return []}}
function rememberSearch(term){const list=[term,...recentSearches().filter(x=>x.toLowerCase()!==term.toLowerCase())].slice(0,8);
  try{localStorage.setItem('remote.recentSearches',JSON.stringify(list))}catch(e){}}
function searchStatus(state,message){const box=$('src-status');box.dataset.state=state;box.replaceChildren();
  if(state==='loading')box.append(el('div',{class:'spin','aria-label':'正在搜索'}));else if(message)box.append(el('div',{class:'hint',text:message}))}
function renderRecent(){const list=$('src-list'),recent=recentSearches();list.replaceChildren();
  searchStatus(recent.length?'':'hint',recent.length?'':'搜索歌曲、专辑或艺人；选中的歌曲会在电脑上的 Apple Music 中播放');
  if(!recent.length)return;
  list.append(el('div',{class:'section-head'},el('span',{text:'最近搜索'}),el('button',{type:'button',class:'link',text:'清除',
    onclick:()=>{try{localStorage.removeItem('remote.recentSearches')}catch(e){}renderRecent()}})));
  recent.forEach((term,i)=>list.append(el('button',{type:'button',class:'row recent',style:'--i:'+i,onclick:()=>{$('sq').value=term;runSearch(true)}},
    el('span',{class:'recent-icon',icon:SEARCHICON}),el('span',{class:'rt'},el('b',{text:term})))))}
async function runSearch(remember){clearTimeout(searchTimer);const term=$('sq').value.trim();if(!term){renderRecent();return}
  const seq=++searchSeq,kind=searchKind;if(searchController)searchController.abort();
  const controller=new AbortController();searchController=controller;const timer=setTimeout(()=>controller.abort(),15000);
  searchStatus('loading');$('src-list').replaceChildren();
  try{const response=await fetch('/catalog/search?'+new URLSearchParams({term,kind}),{signal:controller.signal,cache:'no-store'});
    const data=await response.json().catch(()=>({}));if(seq!==searchSeq)return;
    if(!response.ok||!data.ok){const error=Error(data.err||'搜索失败，请重试');error.code=data.error;error.retry=data.retry_after;throw error}
    if(remember)rememberSearch(term);renderResults(data.items||[],kind,term)}
  catch(e){if(seq!==searchSeq)return;const box=$('src-status');searchStatus('error',catalogMessage(e));
    box.append(el('button',{type:'button',class:'pill',text:'重试',onclick:()=>runSearch(remember)}))}
  finally{clearTimeout(timer);if(searchController===controller)searchController=null}}
function renderResults(items,kind,term){const list=$('src-list');list.replaceChildren();
  if(!items.length){searchStatus('empty','没有找到与“'+term+'”相关的'+({song:'歌曲',album:'专辑',artist:'艺人'}[kind]));return}
  searchStatus('','');
  items.forEach((item,i)=>{if(kind==='song')list.append(el('div',{class:'row-wrap'},
      el('button',{type:'button',class:'row catalog-play',style:'--i:'+i,'aria-label':'播放 '+item.title+' · '+item.artist,
        onclick:e=>playCatalog({kind:'song',album_id:item.album_id,song_id:item.id},e.currentTarget)},
        artImg(item.artwork,48,'thumb'),el('span',{class:'rt'},el('b',{text:item.title}),el('span',{text:[item.artist,item.album].filter(Boolean).join(' · ')})),
        item.explicit?explicitBadge():null,el('span',{class:'dur',text:item.duration?fmt(item.duration):''})),
      el('button',{type:'button',class:'row-more',title:'查看专辑','aria-label':'查看专辑 '+item.album,icon:CHEV,onclick:()=>openAlbum(item.album_id,item.id)})));
    else if(kind==='album')list.append(el('button',{type:'button',class:'row',style:'--i:'+i,onclick:()=>openAlbum(item.id)},
      artImg(item.artwork,48,'thumb'),el('span',{class:'rt'},el('b',{text:item.title}),el('span',{text:[item.artist,item.year].filter(Boolean).join(' · ')})),
      item.explicit?explicitBadge():null,el('span',{icon:CHEV})));
    else list.append(el('button',{type:'button',class:'row',style:'--i:'+i,onclick:()=>openArtist(item.id)},
      el('span',{class:'avatar','aria-hidden':'true',text:(item.name||'?').slice(0,1)}),el('span',{class:'rt'},el('b',{text:item.name}),el('span',{text:item.genre||'艺人'})),
      el('span',{icon:CHEV})))})}

// Album page (iOS 26.4 layout): cover or square motion artwork, play/shuffle, tracks, notes.
function openAlbum(id,highlight){go({v:'album',id:String(id),highlight:highlight?String(highlight):''})}
function pageError(body,e,retry){body.replaceChildren(el('div',{class:'hint',text:catalogMessage(e)}),
  el('button',{type:'button',class:'pill',style:'display:flex;margin:14px auto 0',text:'重试',onclick:retry}))}
async function enterAlbum(state,restoring){show('album');const body=$('alb-body');
  if(restoring&&state.id&&body.dataset.id===state.id&&body.querySelector('.alb-hero')){body.scrollTop=state.scroll||0;refreshNowHighlight();return}
  body.querySelectorAll('.page-motion').forEach(stopPageMotion);body.dataset.id=state.id||'';setTint('view-alb',null);
  body.replaceChildren(el('div',{class:'spin','aria-label':'正在加载专辑'}));
  try{if(!state.id&&state.now){const entry=await state.now.promise;if(navCurrent!==state)return;
      if(!entry||!entry.match){body.replaceChildren(el('div',{class:'hint',text:'Apple Music 曲库中没有找到当前歌曲'}),
        el('button',{type:'button',class:'pill',style:'display:flex;margin:14px auto 0',text:'在曲库中搜索',
          onclick:()=>{if(lyricTrack)openSearch(lyricTrack.title+' '+lyricTrack.artist,'song')}}));return}
      state.id=entry.match.album_id;state.highlight=entry.match.id;body.dataset.id=state.id}
    const data=await loadCatalog('album',state.id);if(navCurrent!==state)return;renderAlbum(data,state)}
  catch(e){if(navCurrent===state)pageError(body,e,()=>enterAlbum(state,false))}}
function renderAlbum(d,state){const body=$('alb-body');setTint('view-alb',d.artwork&&d.artwork.bg);
  const cover=el('div',{class:'alb-art'},artImg(d.artwork,320,'alb-cover'));
  const motion=d.motion&&d.motion.square,src=motionSource(motion);
  if(src&&motionEnabled&&!reducedMotion.matches&&!motionFailed.has(src)){
    const video=el('video',{class:'page-motion',muted:true,playsinline:true,loop:true,preload:'auto','aria-hidden':'true',disablepictureinpicture:true});
    video.muted=true;video.dataset.src=src;video.motion=motion;video.addEventListener('playing',()=>video.classList.add('on'));
    video.addEventListener('error',()=>{if(video.error)pageMotionFailed(video)});video.src=src;cover.append(video)}
  const tracks=el('div',{class:'tracks'}),multiDisc=new Set(d.tracks.map(t=>t.disc||1)).size>1;let disc=null;
  d.tracks.forEach((t,i)=>{if(multiDisc&&(t.disc||1)!==disc){disc=t.disc||1;tracks.append(el('div',{class:'disc',text:'碟 '+disc}))}
    tracks.append(el('button',{type:'button',class:'track catalog-play','data-song':t.id,disabled:t.disabled,'data-locked':t.disabled?'1':null,
      'aria-label':'播放 '+t.title,onclick:e=>playCatalog({kind:'song',album_id:d.id,song_id:t.id},e.currentTarget)},
      el('span',{class:'tn'},el('span',{class:'num',text:String(t.number||i+1)}),el('span',{class:'np-bars','aria-hidden':'true'},el('i'),el('i'),el('i'))),
      el('span',{class:'rt'},el('b',{text:t.title}),t.artist&&norm(t.artist)!==norm(d.artist)?el('span',{text:t.artist}):null),
      t.explicit?explicitBadge():null,el('span',{class:'dur',text:t.duration?fmt(t.duration):''})))});
  body.replaceChildren(...[el('div',{class:'alb-hero'},cover,el('h1',{class:'page-title',tabindex:'-1',text:d.title}),
      artistLinks(d.artists,d.artist),el('div',{class:'page-sub',text:[d.genre_year,d.explicit?'儿童不宜':''].filter(Boolean).join(' · ')}),
      el('div',{class:'alb-actions'},
        el('button',{type:'button',class:'pill catalog-play',icon:PLAYGLYPH+'<span>播放</span>',onclick:e=>playCatalog({kind:'album',album_id:d.id,shuffle:false},e.currentTarget)}),
        el('button',{type:'button',class:'pill catalog-play',icon:SHUF+'<span>随机播放</span>',onclick:e=>playCatalog({kind:'album',album_id:d.id,shuffle:true},e.currentTarget)}))),
    d.description?expandable(d.description,3):null,tracks,
    el('div',{class:'alb-foot'},[d.release,d.summary,d.copyright,d.label?'唱片公司：'+d.label:''].filter(Boolean).map(text=>el('div',{text})),
      el('button',{type:'button',class:'pill',icon:EXTICON+'<span>在电脑上打开</span>',onclick:e=>openOnComputer('album',d.id,e.currentTarget)})),
    ...(d.more||[]).map(shelf=>shelfSection(shelf.title,shelf.items))].filter(Boolean));
  body.scrollTop=0;refreshNowHighlight();
  const video=body.querySelector('.page-motion');if(video)playPageMotion(video);
  const target=state.highlight&&body.querySelector('.track[data-song="'+state.highlight+'"]');
  if(target){target.scrollIntoView({block:'center'});target.classList.add('flash')}
  try{body.querySelector('.page-title').focus({preventScroll:true})}catch(e){}}
function playPageMotion(video){if(!video.paused||!video.dataset.src)return;const src=video.dataset.src,p=video.play();
  if(p&&p.catch)p.catch(e=>{if(e&&e.name!=='AbortError'&&video.dataset.src===src)pageMotionFailed(video)})}
// Same HLS-then-MP4 fallback as the now-playing video.
function pageMotionFailed(video){motionFailed.add(video.dataset.src);const next=motionSource(video.motion);
  if(next&&next!==video.dataset.src&&!motionFailed.has(next)){video.dataset.src=next;video.src=next;if(view==='album')playPageMotion(video)}
  else stopPageMotion(video)}
function stopPageMotion(video){video.pause();video.removeAttribute('src');try{video.load()}catch(e){}video.remove()}
function artistLinks(artists,name){const box=el('div',{class:'page-artists'}),list=(artists||[]).filter(a=>a&&a.name);
  if(!list.length&&name)box.append(el('span',{text:name}));
  list.forEach((a,i)=>{if(i)box.append(el('span',{text:'、'}));
    box.append(a.id?el('button',{type:'button',class:'link',text:a.name,onclick:()=>openArtist(a.id)}):el('span',{text:a.name}))});return box}
function shelfSection(title,items){return el('section',{class:'shelf'},el('h2',{class:'section-head',text:title||'专辑'}),
  el('div',{class:'shelf-row'},items.map(item=>el('button',{type:'button',class:'tile',onclick:()=>openAlbum(item.id)},
    artImg(item.artwork,140,'tile-art'),el('span',{class:'tile-title',text:item.title}),el('span',{class:'tile-sub',text:item.subtitle||''})))))}

// Artist page (iOS 27): header image fading into the artist's key color.
function openArtist(id){go({v:'artist',id:String(id)})}
async function enterArtist(state,restoring){show('artist');const body=$('art-body');
  if(restoring&&body.dataset.id===state.id&&body.querySelector('.art-hero')){body.scrollTop=state.scroll||0;refreshNowHighlight();return}
  body.dataset.id=state.id;setTint('view-art',null);body.replaceChildren(el('div',{class:'spin','aria-label':'正在加载艺人'}));
  body.style.paddingTop='calc(env(safe-area-inset-top,0px) + 66px)';
  try{const data=await loadCatalog('artist',state.id);if(navCurrent!==state)return;body.style.paddingTop='';renderArtist(data)}
  catch(e){if(navCurrent===state)pageError(body,e,()=>enterArtist(state,false))}}
function renderArtist(d){const body=$('art-body');setTint('view-art',d.color);
  const image=d.wide?artUrl(d.wide,1600,900):artUrl(d.artwork,1000),inner=el('div',{class:'page-inner'});
  if(d.genre)inner.append(el('div',{class:'page-sub',text:d.genre}));
  if(d.bio)inner.append(expandable(d.bio,4));
  if(d.top&&d.top.length){const list=el('div',{class:'tracks top'});
    d.top.forEach(t=>list.append(el('button',{type:'button',class:'track catalog-play','data-song':t.id,'aria-label':'播放 '+t.title,
      onclick:e=>playCatalog({kind:'song',album_id:t.album_id,song_id:t.id},e.currentTarget)},
      artImg(t.artwork,44,'thumb'),el('span',{class:'rt'},el('b',{text:t.title}),el('span',{text:t.subtitle})),
      t.explicit?explicitBadge():null,el('span',{class:'dur',text:t.duration?fmt(t.duration):''}))));
    inner.append(el('h2',{class:'section-head',text:'热门歌曲'}),list)}
  if(d.featured)inner.append(el('h2',{class:'section-head',text:d.featured.heading||'最新发行'}),
    el('button',{type:'button',class:'feature',onclick:()=>openAlbum(d.featured.id)},artImg(d.featured.artwork,84,'tile-art'),
      el('span',{class:'rt'},el('small',{text:d.featured.subtitle||''}),el('b',{text:d.featured.title}))));
  (d.shelves||[]).forEach(shelf=>inner.append(shelfSection(shelf.title,shelf.items)));
  if(d.similar&&d.similar.length)inner.append(el('section',{class:'shelf'},el('h2',{class:'section-head',text:'类似艺人'}),
    el('div',{class:'shelf-row'},d.similar.map(a=>el('button',{type:'button',class:'tile circle',onclick:()=>openArtist(a.id)},
      artImg(a.artwork,108,'tile-art'),el('span',{class:'tile-title',text:a.name}))))));
  body.replaceChildren(el('div',{class:'art-hero'+(image?'':' no-image')},
    image?el('img',{src:image,alt:'',decoding:'async'}):el('div',{class:'art-initial','aria-hidden':'true',text:(d.name||'?').slice(0,1)}),
    el('h1',{class:'page-title',tabindex:'-1',text:d.name})),el('div',{class:'art-tail'},inner));
  body.scrollTop=0;refreshNowHighlight();try{body.querySelector('.page-title').focus({preventScroll:true})}catch(e){}}

// Playing a catalog item starts it in Apple Music on the computer, then
// returns to now playing like queue selection does.
async function playCatalog(request,button){if(catalogPlaying)return;catalogPlaying=true;
  const buttons=[...document.querySelectorAll('.catalog-play')];buttons.forEach(b=>b.disabled=true);
  if(button){button.classList.add('busy');button.setAttribute('aria-busy','true')}
  try{const response=await timedFetch('/catalog/play',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request)},30000);
    const result=await response.json().catch(()=>({}));if(!response.ok||!result.ok)throw Error(result.err||'播放失败，请重试');
    controlError('');beginTrackChange();show('now');setTimeout(poll,350)}
  catch(e){controlError(e.name==='AbortError'?'播放超时，请在电脑上确认 Apple Music 当前歌曲':catalogMessage(e))}
  finally{catalogPlaying=false;buttons.forEach(b=>{b.disabled=b.dataset.locked==='1'});
    if(button){button.classList.remove('busy');button.removeAttribute('aria-busy')}}}
async function openOnComputer(kind,id,button){button.disabled=true;
  try{const response=await timedFetch('/catalog/open',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind,id})},15000);
    const result=await response.json().catch(()=>({}));if(!response.ok||!result.ok)throw Error(result.err||'无法在电脑上打开');
    controlError('');const label=button.querySelector('span');if(label){label.textContent='已在电脑上打开';setTimeout(()=>{label.textContent='在电脑上打开'},2500)}}
  catch(e){controlError(catalogMessage(e))}finally{button.disabled=false}}

// Current song: matched in the catalog after each track change; the title
// opens its album, the artist opens the artist page (a chooser for several).
function catalogTrack(s){const key=s&&s.ok&&s.loaded!==false&&s.title&&s.dur>0?lyricKey:'';if(key===nowCatalog.key)return;
  cancelCatalogTrack();
  const entry={key,state:key?'pending':'none',match:null,album:null,promise:null,controller:new AbortController(),retryTimer:0,startTimer:0};nowCatalog=entry;
  setNowLinks();updateThemeColor();motionUpdate();refreshNowHighlight();if(!key)return;
  const params=new URLSearchParams({title:s.title,artist:s.artist||'',album:s.album||'',duration:String(s.dur)});
  const attempt=async retried=>{let retry=false;
    try{const response=await timedFetch('/catalog/resolve?'+params,{signal:entry.controller.signal},16000);const data=await response.json().catch(()=>({}));
      if(entry!==nowCatalog)return entry;
      if(!response.ok||!data.ok){entry.state='error';retry=true}
      else{entry.match=data.match||null;entry.state=entry.match?'matched':'none';retry=!entry.match;
        // Presentation albums never supply song IDs for navigation or highlighting.
        const albumId=Object.prototype.hasOwnProperty.call(data,'artwork_album_id')?data.artwork_album_id:entry.match&&entry.match.album_id;
        if(albumId)try{const album=await loadCatalog('album',albumId,entry.controller.signal);
          if(entry!==nowCatalog)return entry;entry.album=album}catch(e){retry=true}}}
    catch(e){if(entry!==nowCatalog)return entry;entry.state='error';retry=true}
    if(entry===nowCatalog){setNowLinks();updateThemeColor();motionUpdate();refreshNowHighlight();
      if(retry&&!retried)entry.retryTimer=setTimeout(()=>{if(entry===nowCatalog)entry.promise=attempt(true)},65000)}
    return entry};
  entry.promise=new Promise(resolve=>{entry.cancelStart=()=>resolve(entry);
    entry.startTimer=setTimeout(()=>resolve(attempt(false)),500)})}
function cancelCatalogTrack(){clearTimeout(nowCatalog.startTimer);clearTimeout(nowCatalog.retryTimer);
  if(nowCatalog.cancelStart)nowCatalog.cancelStart();if(nowCatalog.controller)nowCatalog.controller.abort()}
function setNowLinks(){const active=!!nowCatalog.key;
  [['t','打开专辑：'],['a','查看艺人：']].forEach(([id,prefix])=>{const node=$(id);node.tabIndex=active?0:-1;
    node.setAttribute('aria-disabled',String(!active));node.classList.toggle('linkable',active);
    if(active)node.setAttribute('aria-label',prefix+node.textContent);else node.removeAttribute('aria-label')})}
function refreshNowHighlight(){const id=nowCatalog.match&&nowCatalog.match.id;
  document.querySelectorAll('.track[data-song]').forEach(button=>{const on=!!id&&button.dataset.song===id;button.classList.toggle('current',on);
    if(on)button.setAttribute('aria-current','true');else button.removeAttribute('aria-current')})}
function openNowAlbum(){const entry=nowCatalog;if(!entry.key)return;
  if(entry.match)openAlbum(entry.match.album_id,entry.match.id);else if(entry.state==='pending')go({v:'album',id:'',now:entry});
  else if(lyricTrack)openSearch(lyricTrack.title+' '+lyricTrack.artist,'song')}
async function openNowArtist(){let entry=nowCatalog;if(!entry.key)return;
  if(entry.state==='pending'&&entry.promise){$('a').setAttribute('aria-busy','true');
    try{await entry.promise}finally{$('a').removeAttribute('aria-busy')}if(entry!==nowCatalog)return}
  const track=entry.album&&entry.match&&(entry.album.tracks||[]).find(t=>t.id===entry.match.id);
  let artists=((track&&track.artists&&track.artists.length?track.artists:entry.match&&entry.album&&entry.album.artists)||[]).filter(a=>a&&a.id);
  if(!artists.length&&entry.match&&entry.match.artist_id)artists=[{id:entry.match.artist_id,name:entry.match.artist}];
  if(artists.length>1)showArtistChoice(artists);else if(artists.length)openArtist(artists[0].id);
  else if(lyricTrack&&lyricTrack.artist)openSearch(lyricTrack.artist,'artist')}
function showArtistChoice(artists){$('artist-choice-list').replaceChildren(...artists.map((a,i)=>el('button',{type:'button',class:'row',style:'--i:'+i,
    onclick:()=>{closeArtistChoice();openArtist(a.id)}},el('span',{class:'avatar','aria-hidden':'true',text:a.name.slice(0,1)}),el('span',{class:'rt'},el('b',{text:a.name})))));
  $('artist-choice').hidden=false;$('artist-choice-list').querySelector('button').focus()}
function closeArtistChoice(){if($('artist-choice').hidden)return;$('artist-choice').hidden=true;try{$('a').focus({preventScroll:true})}catch(e){}}
$('artist-choice').addEventListener('click',e=>{if(e.target===$('artist-choice'))closeArtistChoice()});
['click','keydown'].forEach(type=>[['t',openNowAlbum],['a',openNowArtist]].forEach(([id,open])=>$(id).addEventListener(type,e=>{
  if(type==='keydown'){if(e.key!=='Enter'&&e.key!==' ')return;e.preventDefault()}if($(id).classList.contains('linkable'))open()})));

// Motion artwork: muted inline loop of the album's motion cover. Phones in
// portrait show the tall video full screen (#art only hidden, its box stays);
// other layouts lay the square video over #art like the lyrics panel. Only
// while playing on the now-playing page with lyrics closed; any failure,
// refused play() (e.g. low power mode), reduced motion or the 调整 switch
// falls back to the fluid backdrop. Native HLS when supported, else MP4.
function motionShape(){return !matchMedia('(min-width:760px) and (min-height:360px)').matches&&innerHeight>innerWidth?'tall':'square'}
// Chrome/Edge 156 answer "maybe" for HLS yet reject Apple's motion playlists,
// so a failed or stalled HLS source falls back to the server-picked MP4.
function motionSource(entry){if(!entry)return '';
  if(hlsPlayable&&entry.hls&&!motionFailed.has(entry.hls))return entry.hls;
  return (entry.file&&entry.file.url)||''}
function motionTarget(){const shape=motionShape(),album=nowCatalog.album,entry=album&&album.motion?album.motion[shape]:null,src=motionSource(entry);
  const video=$(shape==='tall'?'bg-motion':'art-motion'),other=$(shape==='tall'?'art-motion':'bg-motion');
  const want=!!src&&motionEnabled&&!reducedMotion.matches&&!lyricsVisible&&playing&&!document.hidden&&view==='now'&&!motionFailed.has(src);
  return {shape,src,video,other,want}}
function motionUpdate(){const m=motionTarget();hideMotion(m.other,true);
  if(!m.want){hideMotion(m.video,!m.src||motionFailed.has(m.src)||m.video.dataset.src!==m.src);return}
  if(m.video.dataset.src!==m.src){hideMotion(m.video,true);m.video.dataset.src=m.src;m.video.src=m.src;
    const video=m.video,src=m.src;clearTimeout(motionWatch);
    // A source that never delivers a frame counts as failed (some HLS loads stall silently).
    motionWatch=setTimeout(()=>{if(video.dataset.src===src&&video.readyState<2&&motionTarget().want)motionFail(video,src)},9000)}
  if(m.shape==='square')positionMotion();
  if(m.video.paused){const p=m.video.play(),src=m.src;
    // AbortError only means pause() or a new source interrupted this play().
    if(p&&p.catch)p.catch(e=>{if(e&&e.name!=='AbortError'&&m.video.dataset.src===src)motionFail(m.video,src)})}
  else if(m.video.readyState>2)showMotion(m.video)}
function motionFail(video,src){motionFailed.add(src);hideMotion(video,true);motionUpdate()}
function showMotion(video){video.classList.add('on');
  if(video.id==='bg-motion'){document.body.classList.add('motion-tall')}else{document.body.classList.add('motion-square');positionMotion()}}
function hideMotion(video,drop){video.classList.remove('on');
  if(video.id==='bg-motion'){if(document.body.classList.contains('motion-tall')){document.body.classList.remove('motion-tall');bgKick()}}
  else document.body.classList.remove('motion-square');
  if(!video.paused)video.pause();
  if(drop&&video.dataset.src){delete video.dataset.src;video.removeAttribute('src');try{video.load()}catch(e){}}}
['bg-motion','art-motion'].forEach(id=>{const video=$(id);
  video.addEventListener('playing',()=>{const m=motionTarget();if(m.want&&m.video===video&&video.dataset.src===m.src)showMotion(video);else hideMotion(video,false)});
  // video.error is reset by the next load, so a late event for a replaced source is ignored.
  video.addEventListener('error',()=>{if(video.dataset.src&&video.error)motionFail(video,video.dataset.src)})});
// Same box as the static cover, untransformed (the cover scales while paused).
function positionMotion(){const video=$('art-motion');if(!video.dataset.src)return;
  const art=$('art'),r=art.getBoundingClientRect();if(!r.width||!r.height)return;
  const style=getComputedStyle(art);let scale=1;
  const matrix=/matrix\\(([^)]+)\\)/.exec(style.transform||'');if(matrix){const v=matrix[1].split(',').map(Number);scale*=Math.hypot(v[0],v[1])||1}
  if(style.scale&&style.scale!=='none')scale*=parseFloat(style.scale)||1;
  let zoom=1;for(let node=video;node;node=node.parentElement)zoom*=Number(getComputedStyle(node).zoom)||1;
  const side=Math.min(r.width,r.height)/scale,x=r.left+r.width/2-side/2,y=r.top+r.height/2-side/2;
  video.style.left=x/zoom+'px';video.style.top=y/zoom+'px';video.style.width=video.style.height=side/zoom+'px'}
function toggleMotion(){motionEnabled=!motionEnabled;try{localStorage.setItem('remote.motionArtwork',String(motionEnabled))}catch(e){}
  $('motion-toggle').setAttribute('aria-pressed',String(motionEnabled));if(motionEnabled)motionFailed.clear();motionUpdate()}
$('motion-toggle').setAttribute('aria-pressed',String(motionEnabled));
window.addEventListener('resize',motionUpdate);

let queueRequest=0,queuePlaying=false;
async function timedFetch(url,options={},timeoutMs=15000){const controller=new AbortController();
  const signal=options.signal,abort=()=>controller.abort();
  if(signal){if(signal.aborted)abort();else signal.addEventListener('abort',abort,{once:true})}
  const timer=setTimeout(()=>controller.abort(),timeoutMs);
  try{const response=await fetch(url,{...options,signal:controller.signal,cache:'no-store'});
    // fetch resolves at headers. Keep the deadline until the whole body arrives.
    const body=await response.arrayBuffer();
    return new Response([204,205,304].includes(response.status)?null:body,
      {status:response.status,statusText:response.statusText,headers:response.headers});
  }finally{clearTimeout(timer);if(signal)signal.removeEventListener('abort',abort)}}
function controlError(message){$('control-error').textContent=message;$('control-error').hidden=!message}
async function command(x){try{const response=await timedFetch('/cmd/'+x,{method:'POST'});
  if(!response.ok){const body=await response.json().catch(()=>({}));throw Error(body.err||'控制失败，请重试')}
  controlError('');return true;
}catch(e){controlError(e.name==='AbortError'?'控制超时，请检查 Apple Music 后重试':(e.message||'无法连接遥控服务'));return false}}
async function openQue(){if(queuePlaying)return;show('que');const request=++queueRequest,el=$('que-list');el.innerHTML='<div class=spin></div>';
  try{const r=await(await timedFetch('/queue')).json();if(request!==queueRequest)return;if(!r.ok)
      throw new Error((r.err||'读取失败')+(r.restore_err?'（恢复面板：'+r.restore_err+'）':''));
    el.innerHTML=r.items.map((t,i)=>'<button type=button class=row style="--i:'+i+'"><span class=qidx>'+(i+1)+'</span>'
      +'<span class=rt><b>'+esc(t.t)+'</b><span>'+esc(t.a)+'</span></span>'
      +'<span class=dur>'+esc(t.d)+'</span></button>').join('')
      ||'<div class=hint>待播队列为空</div>';
    el.querySelectorAll('.row').forEach((button,index)=>{
      button.setAttribute('aria-label','播放 '+r.items[index].t+' · '+r.items[index].a);
      button.onclick=()=>playQueueItem(index,r.revision,button)})}
  catch(e){if(request!==queueRequest)return;const hint=document.createElement('div');hint.className='hint';
    hint.textContent='无法读取待播队列：'+(e instanceof Error?e.message:String(e));
    const retry=document.createElement('button');retry.textContent='重试';retry.onclick=openQue;
    el.replaceChildren(hint,retry)}}

async function playQueueItem(index,revision,button){if(queuePlaying)return;queuePlaying=true;
  const request=queueRequest,rows=$('que-list').querySelectorAll('.row');
  rows.forEach(row=>row.disabled=true);button.classList.add('busy');button.setAttribute('aria-busy','true');
  let stale=false;
  try{const response=await timedFetch('/queue/play',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({index,revision})});
    const result=await response.json();stale=response.status===409;
    if(!response.ok||!result.ok)throw Error(result.err||'选曲失败，请重试');
    controlError(result.restore_err?'已切换歌曲，但恢复电脑队列面板失败：'+result.restore_err:'');
    beginTrackChange();if(request===queueRequest&&view==='que')show('now');setTimeout(poll,350);
  }catch(e){controlError(e.name==='AbortError'?'选曲超时，请先确认当前播放歌曲再重试':(e.message||'无法连接遥控服务'))}
  finally{queuePlaying=false;rows.forEach(row=>row.disabled=false);button.classList.remove('busy');button.removeAttribute('aria-busy')}
  if(stale&&request===queueRequest&&view==='que')await openQue();
}

async function c(x){if(x==='next'||x==='prev')beginTrackChange();
  // Optimistic icon only; the playback clock still follows the server.
  if(x==='toggle'){ppHold=Date.now()+1500;document.body.classList.toggle('playing')}
  if(x==='mute')document.body.classList.toggle('muted');
  if(!await command(x)){ppHold=0;setPlayingClass()};setTimeout(poll,350)}
function setPlayingClass(){if(Date.now()>ppHold||document.body.classList.contains('playing')===playing){
  ppHold=0;document.body.classList.toggle('playing',playing)}
  motionUpdate()}
let modes={shuffle:false,repeat:'none'};
function renderModes(){$('shf').innerHTML=SHUF;$('shf').classList.toggle('on',!!modes.shuffle);
  $('rpt').innerHTML=modes.repeat==='one'?REP1:REP;$('rpt').classList.toggle('on',modes.repeat!=='none')}
async function loadModes(){try{const m=await(await timedFetch('/modes')).json();if(m.ok)modes=m}catch(e){}renderModes()}
async function mode(x){
  const previous={...modes};
  // optimistic update; the button state in the app lags a moment behind
  if(x==='shuffle')modes.shuffle=!modes.shuffle;
  else modes.repeat=modes.repeat==='none'?'all':modes.repeat==='all'?'one':'none';
  renderModes();
  if(!await command(x)){modes=previous;renderModes()};setTimeout(loadModes,700)}
let vt;
function vset(v){dragTs=Date.now();fill($('vr'),+v,100);clearTimeout(vt);vt=setTimeout(()=>command('vol/'+v),120)}
function fmt(x){x=Math.max(0,x)|0;return(x/60|0)+':'+String(x%60).padStart(2,'0')}
function fill(el,v,max){el.style.setProperty('--fill',(max>0?Math.max(0,Math.min(100,v/max*100)):0)+'%')}
function setText(id,text){const el=$(id);if(el.textContent!==text)el.textContent=text}
// Keep #pr in step so the 75 ms lyric clock (which reads it while scrubbing) agrees.
function scrub(v){scrubbing=true;$('pr').value=v;$('prog').classList.add('scrubbing');$('cur').textContent=fmt(+v);fill($('pr'),+v,dur);syncLyrics(+v)}
async function seekTo(v){playbackEpoch++;scrubbing=false;$('prog').classList.remove('scrubbing');
  pos=+v;posTs=performance.now();seekTs=Date.now();clockRecalibrate=true;
  syncLyrics(+v,true);
  const epoch=playbackEpoch;
  if(!await command('seek/'+(v|0))&&epoch===playbackEpoch){seekTs=0;clockRecalibrate=true;await poll()}
  setTimeout(poll,600)}
function statusPosition(s,sent,received){
  const age=Math.max(0,s.sample_age_ms||0),processing=Math.max(age,s.processing_ms||age);
  const transit=Math.min(2,(age+Math.max(0,received-sent-processing)/2)/1000);
  return Math.max(0,Math.min(s.dur,s.pos+(s.playing?transit:0)))}
async function poll(){if(pollBusy)return;pollBusy=true;const epoch=playbackEpoch;try{
  const sent=performance.now();
  const s=await(await timedFetch('/status',{},8000)).json();
  const received=performance.now(),previousPosition=playbackPosition(),previousPlaying=playing;
  if(s.lyric_settings&&s.lyric_settings.ok){
    if(offsetConfirmed===null)offsetError='';acceptLyricSettings(s.lyric_settings)}
  else if(s.lyric_settings&&!s.lyric_settings.ok){offsetError='设置读取失败，请重试';renderLyricOffset()}
  if(epoch!==playbackEpoch)return;
  if(!s.ok){setText('t','Apple Music 未运行');setText('a','');playing=false;dur=0;pos=0;scrubbing=false;
    $('prog').classList.remove('scrubbing');setPlayingClass();updateLyricTrack(null);last='';loadArtwork('');tick();bgKick();return}
  // Session without a loaded song: no lyric or catalog lookups for "未在播放".
  if(s.loaded===false){setText('t','请在 Apple Music 中播放歌曲');setText('a','');playing=false;dur=0;pos=0;scrubbing=false;
    nowTitle='';$('prog').classList.remove('scrubbing');setPlayingClass();document.body.classList.toggle('muted',!!s.muted);
    if(s.vol!=null&&Date.now()-dragTs>1500){$('vr').value=s.vol;fill($('vr'),s.vol,100)}
    updateLyricTrack(null);if(last!==''||artworkJob){last='';loadArtwork('')}tick();bgKick();return}
  const changed=lyricKey!==JSON.stringify({title:s.title||'',artist:s.lyric_artist||s.artist||'',album:s.album||'',duration:Math.round(s.dur||0)});
  if((s.title||'')!==nowTitle){const info=$('t').parentElement;info.classList.remove('swap');void info.offsetWidth;info.classList.add('swap')}
  nowTitle=s.title||'';
  setText('t',s.title||'请在 Apple Music 中播放歌曲');setText('a',s.artist||'');
  playing=s.playing;document.body.classList.toggle('paused',!playing);setPlayingClass();
  document.body.classList.toggle('muted',!!s.muted);
  dur=s.dur;
  if(changed){scrubbing=false;seekTs=0}
  if(!scrubbing&&(changed||Date.now()-seekTs>3000)){
    const sample=String(s.timeline_sample||'');
    const measured=statusPosition(s,sent,received);
    const tolerance=Math.max(.15,(s.position_precision_ms||150)/1000);
    // Repeated SMTC samples must not reset a running clock. Small sampling
    // jitter is ignored; seek/track/pause/resume/visibility always recalibrate.
    if(changed||clockRecalibrate||previousPlaying!==s.playing||!s.playing||
        (sample!==clockSample&&Math.abs(measured-previousPosition)>tolerance)||
        (!sample&&Math.abs(measured-previousPosition)>tolerance)){
      pos=measured;posTs=received;clockRecalibrate=false}
    clockSample=sample}
  updateLyricTrack(s);
  if(lyricsVisible&&!lyricLoaded&&!lyricRequest)requestLyrics();
  syncLyrics(playbackPosition());
  if(s.vol!=null&&Date.now()-dragTs>1500){$('vr').value=s.vol;fill($('vr'),s.vol,100)}
  const k=lyricKey;
  if(k!==last){last=k;loadArtwork(k)}
  bgKick();
  tick();
}catch(e){pos=playbackPosition();posTs=performance.now();playing=false;clockRecalibrate=true;setPlayingClass();bgKick()}
finally{pollBusy=false}}
function tick(){
  if(dur>0){$('pr').max=dur|0;setText('tot',fmt(dur));$('prog').style.visibility='visible';
    if(!scrubbing){let p=playbackPosition();
      $('pr').value=p|0;setText('cur',fmt(p));fill($('pr'),p,dur)}}
  else $('prog').style.visibility='hidden'}
let lyricTickTimer=0;
function startLyricClock(){if(lyricTickTimer||document.hidden)return;
  lyricTickTimer=setInterval(()=>syncLyrics(playbackPosition()),75)}
document.addEventListener('visibilitychange',()=>{if(document.hidden){clearInterval(lyricTickTimer);lyricTickTimer=0}
  else{clockRecalibrate=true;poll();startLyricClock();bgKick()}motionUpdate()});
window.addEventListener('pageshow',()=>{clockRecalibrate=true;poll();startLyricClock()});
$('qbtn').innerHTML=QUEUEICON;$('sbtn').innerHTML=SEARCHICON;fill($('vr'),+$('vr').value,100);
show('now');renderModes();readLyricSettings();setInterval(poll,2000);setInterval(tick,250);startLyricClock();poll();loadModes();bgKick();
</script>"""


# --------------------------------------------------------------------------
# System volume (pycaw / Core Audio)
# --------------------------------------------------------------------------

def _volume_endpoint():
    # ThreadingHTTPServer handles each request on a fresh thread, so COM must
    # be initialized here rather than once at startup.
    CoInitialize()
    return AudioUtilities.GetSpeakers().EndpointVolume


def get_volume() -> tuple[int, bool]:
    """Return (volume percent 0-100, muted)."""
    endpoint = _volume_endpoint()
    return round(endpoint.GetMasterVolumeLevelScalar() * 100), bool(endpoint.GetMute())


def set_volume(percent: int) -> None:
    _volume_endpoint().SetMasterVolumeLevelScalar(max(0, min(100, percent)) / 100, None)


def toggle_mute() -> None:
    endpoint = _volume_endpoint()
    endpoint.SetMute(not endpoint.GetMute(), None)


# --------------------------------------------------------------------------
# Playback state and transport (SMTC / winsdk)
# --------------------------------------------------------------------------

async def _session() -> Optional[Session]:
    manager = await SessionManager.request_async()
    for session in manager.get_sessions():
        if "applemusic" in (session.source_app_user_model_id or "").lower():
            return session
    # Never send Apple Music commands to a browser or another media app.
    return None


async def _status() -> dict:
    request_started = time.monotonic()
    session = await _session()
    if not session:
        return {"ok": False}
    props = await session.try_get_media_properties_async()
    playing = session.get_playback_info().playback_status == PlaybackStatus.PLAYING
    timeline = session.get_timeline_properties()
    # SMTC position is anchored at last_updated_time, not necessarily "now".
    sampled_at = time.monotonic()
    from datetime import datetime, timezone
    updated = timeline.last_updated_time
    age = max(0, (datetime.now(timezone.utc) - updated).total_seconds())
    position = timeline.position.total_seconds() + (age if playing else 0)
    try:
        vol, muted = get_volume()
    except Exception:
        vol, muted = None, False
    # Apple Music 1.1792 keeps a session with the title "未在播放" (no artist,
    # zero duration) when nothing is loaded. "正在连接…" also has no duration
    # yet but is playing, so only a stopped session without metadata is idle.
    if timeline.end_time.total_seconds() <= 0 and not (props.artist or "").strip() and not playing:
        return {"ok": True, "loaded": False, "title": "", "artist": "", "lyric_artist": "", "album": "",
                "playing": False, "pos": 0, "dur": 0, "vol": vol, "muted": muted}
    artist, album = lyric_metadata(props.artist, props.album_title)
    return {"ok": True, "loaded": True, "title": props.title, "artist": props.artist,
            "lyric_artist": artist, "album": album,
            "playing": playing,
            "pos": max(0, min(timeline.end_time.total_seconds(), position)),
            "timeline_sample": updated.isoformat(),
            "sample_age_ms": (time.monotonic() - sampled_at) * 1000,
            "processing_ms": (time.monotonic() - request_started) * 1000,
            "position_precision_ms": 1000 if timeline.position.microseconds == 0 else 150,
            "dur": timeline.end_time.total_seconds(),
            "vol": vol, "muted": muted}


async def _artwork() -> Optional[bytes]:
    session = await _session()
    if not session:
        return None
    props = await session.try_get_media_properties_async()
    if not props.thumbnail:
        return None
    stream = await props.thumbnail.open_read_async()
    buf = Buffer(stream.size)
    await stream.read_async(buf, stream.size, InputStreamOptions.READ_AHEAD)
    data = bytearray(buf.length)
    DataReader.from_buffer(buf).read_bytes(data)
    return bytes(data)


async def _transport(command: str) -> None:
    session = await _session()
    if not session:
        raise RuntimeError("Apple Music 未运行或尚未载入歌曲")
    accepted = await {"toggle": session.try_toggle_play_pause_async,
           "next": session.try_skip_next_async,
           "prev": session.try_skip_previous_async}[command]()
    if not accepted:
        raise RuntimeError("Apple Music 未接受播放指令，请先在应用中载入歌曲")


# --------------------------------------------------------------------------
# UI Automation bridge (seek, shuffle, repeat, playlists)
#
# Apple Music's SMTC integration rejects position changes and does not expose
# shuffle/repeat or any library access, so these features drive the app's own
# window through UI Automation patterns. Queue selection additionally uses
# a hit-tested double click when the app exposes no working InvokePattern.
# UIA operations must not interleave, hence the global lock.
# --------------------------------------------------------------------------

UIA_LOCK = threading.Lock()
PLAYLISTS: list[dict] = []   # [{"aid": <AutomationId>, "name": <str>}]
TRACK_CACHE: dict[int, list[dict]] = {}  # playlist index -> parsed tracks


def _app_windows() -> list:
    # The remote's own browser title also contains 'Apple Music'. Restrict UIA
    # to the actual app process, independent of title, language and Z order.
    pids = {p.pid for p in uiautomation.GetProcesses(detailedInfo=False)
            if p.exeName.casefold() == "applemusic.exe"}
    return [window for window in uiautomation.GetRootControl().GetChildren()
            if (window.ControlTypeName == "WindowControl" and window.ProcessId in pids
                and window.ClassName == "WinUIDesktopWin32WindowClass")]


def _is_main_window(window) -> bool:
    try:
        return any(uiautomation.Control(searchFromControl=window, AutomationId=aid,
                                        searchDepth=6).Exists(0, 0)
                   for aid in ("TransportBar", "NavView"))
    except Exception:
        return False


def _window() -> uiautomation.WindowControl:
    # Apple Music 1.1792's mini player is a second window of the same class
    # listed before the main one. The main window stays in the UIA tree while
    # minimized, and its transport controls keep working, so always prefer it.
    windows = _app_windows()
    if not windows:
        raise RuntimeError("找不到 Apple Music 应用窗口，请打开 Apple Music（非网页播放器）")
    if len(windows) > 1:
        for window in windows:
            if _is_main_window(window):
                return window
    return windows[0]


def _mini_player_scrubber(main):
    """The mini player's own progress slider, used when LCDScrubber is missing."""
    for window in _app_windows():
        if main is not None and window.NativeWindowHandle == getattr(main, "NativeWindowHandle", None):
            continue
        slider = uiautomation.SliderControl(searchFromControl=window, AutomationId=MINI_SCRUBBER_AUTOMATION_ID)
        if slider.Exists(0.5):
            return slider
    return None


def _load_playlists(window) -> None:
    """Read user playlists from the sidebar. Requires the sidebar's
    'Playlists' section to be expanded (it is by default)."""
    PLAYLISTS.clear()
    for control, _ in uiautomation.WalkControl(window, maxDepth=12):
        if (control.ControlTypeName == "ListItemControl"
                and "IKIND:ePlaylist" in (control.AutomationId or "")):
            PLAYLISTS.append({"aid": control.AutomationId, "name": control.Name})


def _open_playlist(window, index: int):
    """Select a playlist in the sidebar and return its track ListControl."""
    if not PLAYLISTS:
        _load_playlists(window)
    playlist = PLAYLISTS[index]
    item = uiautomation.ListItemControl(searchFromControl=window,
                                        AutomationId=playlist["aid"])
    item.GetSelectionItemPattern().Select()
    # The track list is loaded once its header row ("<name> N items ...")
    # matches the selected playlist.
    for _ in range(20):
        time.sleep(0.4)
        for control, _ in uiautomation.WalkControl(window, maxDepth=16):
            if control.ControlTypeName == "ListControl":
                kids = control.GetChildren()
                if kids and (kids[0].Name or "").startswith(playlist["name"]):
                    return control
    raise RuntimeError("track list did not load")


def _parse_row(row) -> Optional[dict]:
    """Extract title/artist/duration from a track row's text elements."""
    kids = row.GetChildren()
    grid = kids[0] if kids else None
    texts = [t.Name for t in (grid.GetChildren() if grid else [])
             if t.ControlTypeName == "TextControl" and t.Name]
    if not texts:
        return None
    duration = texts[-1] if TRACK_DURATION_RE.match(texts[-1]) else ""
    return {"t": texts[0], "a": texts[1] if len(texts) > 2 else "",
            "d": duration, "id": row.Name}


def _walk_rows(track_list, step: int = 25) -> Iterator[list]:
    """Yield visible rows while scrolling through a virtualized list.

    Only rendered rows exist in the UIA tree, so the list is scrolled in
    ``step``-percent increments and re-read at each stop. Large playlists
    (thousands of tracks) will take proportionally longer on first read.
    """
    try:
        scroll = track_list.GetScrollPattern()
        if not scroll.VerticallyScrollable:
            scroll = None
    except Exception:
        scroll = None
    if scroll is not None:
        scroll.SetScrollPercent(-1, 0)
        time.sleep(0.2)
    percent = 0
    while True:
        yield track_list.GetChildren()
        if scroll is None or percent >= 100:
            return
        percent = min(percent + step, 100)
        scroll.SetScrollPercent(-1, percent)
        time.sleep(0.3)


def _collect_tracks(track_list) -> list[dict]:
    seen: set[str] = set()
    items: list[dict] = []
    for rows in _walk_rows(track_list):
        for row in rows:
            name = row.Name or ""
            if name.startswith("Track ") and name not in seen:
                seen.add(name)
                parsed = _parse_row(row)
                if parsed:
                    items.append(parsed)
    return items


def _invoke_row_play(window, row) -> None:
    """Play a song row: open its "More" (…) menu and invoke 'Play <title>'."""
    try:
        row.GetScrollItemPattern().ScrollIntoView()
        time.sleep(0.3)
    except Exception:
        pass
    grid = row.GetChildren()[0]
    buttons = [b for b in grid.GetChildren() if b.ControlTypeName == "ButtonControl"]
    if not buttons:
        raise RuntimeError("row has no menu button")
    buttons[-1].GetInvokePattern().Invoke()
    time.sleep(0.8)
    texts = [t.Name for t in grid.GetChildren()
             if t.ControlTypeName == "TextControl" and t.Name]
    title = texts[0] if texts else ""
    for item, _ in uiautomation.WalkControl(window, maxDepth=18):
        if item.ControlTypeName == "MenuItemControl":
            name = item.Name or ""
            # Match 'Play "<title>"' by title so it works in any locale;
            # the quoted form is the English fallback for very short titles.
            if (title and title[:10] in name) or name.startswith("Play “"):
                item.GetInvokePattern().Invoke()
                return
    raise RuntimeError("Play menu item not found")


def _play_track(window, track_list, row_id: str) -> None:
    row = None
    for rows in _walk_rows(track_list):
        row = next((r for r in rows if r.Name == row_id), None)
        if row:
            break
    if not row:
        raise RuntimeError("track not found in playlist")
    _invoke_row_play(window, row)


def _search_section(window, name: str):
    """Find a named section ('Songs', 'Top Results', ...) on the results page."""
    for control, _ in uiautomation.WalkControl(window, maxDepth=8):
        if control.ControlTypeName == "GroupControl" and control.Name == "Content":
            for section, _ in uiautomation.WalkControl(control, maxDepth=3):
                if (section.ControlTypeName == "ListItemControl"
                        and section.Name == name):
                    return section
            return None
    return None


def _do_search(window, query: str):
    """Type into the sidebar search box, run the search, return the Songs
    section (or None when the results contain no songs)."""
    box = uiautomation.EditControl(searchFromControl=window, AutomationId="TextBox")
    if not box.Exists(2):
        raise RuntimeError("search box not found")
    box.GetValuePattern().SetValue(query)
    uiautomation.ButtonControl(searchFromControl=window,
                               AutomationId="QueryButton").GetInvokePattern().Invoke()
    time.sleep(1.0)  # let a previous results page clear before polling
    for _ in range(20):
        time.sleep(0.4)
        songs = _search_section(window, "Songs")
        if songs:
            return songs
        if _search_section(window, "Top Results"):
            time.sleep(0.6)  # sections render one by one; give Songs one more beat
            return _search_section(window, "Songs")
    raise RuntimeError("search results did not load")


def _search_rows(section) -> list:
    rows = []
    for control, _ in uiautomation.WalkControl(section, maxDepth=6):
        if (control.ControlTypeName == "ListItemControl"
                and control.Name and control.Name != section.Name):
            rows.append(control)
    return rows


def _transport_button(window, automation_id: str):
    button = uiautomation.ButtonControl(searchFromControl=window,
                                        AutomationId=automation_id)
    if not button.Exists(2):
        raise RuntimeError(automation_id + " not found")
    return button


def seek(seconds: int) -> None:
    """Seek by setting the transport bar's scrubber slider directly."""
    with UIA_LOCK, uiautomation.UIAutomationInitializerInThread():
        window = _window()
        slider = uiautomation.SliderControl(searchFromControl=window,
                                            AutomationId=SCRUBBER_AUTOMATION_ID)
        if not slider.Exists(1):
            slider = _mini_player_scrubber(window)
        if slider is None:
            raise RuntimeError("Apple Music 进度条不可用，请先在 Apple Music 中载入歌曲")
        pattern = slider.GetPattern(uiautomation.PatternId.RangeValuePattern)
        if not pattern or pattern.IsReadOnly:
            raise RuntimeError("当前歌曲不支持调整播放进度")
        pattern.SetValue(max(pattern.Minimum, min(pattern.Maximum, seconds)))


def toggle_shuffle() -> None:
    with UIA_LOCK, uiautomation.UIAutomationInitializerInThread():
        _transport_button(_window(), "ShuffleButton").GetTogglePattern().Toggle()


def cycle_repeat() -> None:
    """Advance repeat mode: off -> repeat all -> repeat one -> off."""
    with UIA_LOCK, uiautomation.UIAutomationInitializerInThread():
        button = _transport_button(_window(), "RepeatButton")
        button.GetLegacyIAccessiblePattern().DoDefaultAction()


def api_modes() -> dict:
    with UIA_LOCK, uiautomation.UIAutomationInitializerInThread():
        window = _window()
        shuffle = (_transport_button(window, "ShuffleButton")
                   .GetTogglePattern().ToggleState == 1)
        repeat = REPEAT_STATES.get(_transport_button(window, "RepeatButton").Name,
                                   "none")
    return {"ok": True, "shuffle": shuffle, "repeat": repeat}


def api_playlists() -> dict:
    with UIA_LOCK, uiautomation.UIAutomationInitializerInThread():
        _load_playlists(_window())
    return {"ok": True, "items": [{"name": p["name"]} for p in PLAYLISTS]}


def api_tracks(index: int, fresh: bool = False) -> dict:
    if not fresh and index in TRACK_CACHE:
        return {"ok": True, "items": TRACK_CACHE[index]}
    with UIA_LOCK, uiautomation.UIAutomationInitializerInThread():
        window = _window()
        track_list = _open_playlist(window, index)
        TRACK_CACHE[index] = _collect_tracks(track_list)
    return {"ok": True, "items": TRACK_CACHE[index]}


def api_play(index: int, row_id: str) -> dict:
    with UIA_LOCK, uiautomation.UIAutomationInitializerInThread():
        window = _window()
        track_list = _open_playlist(window, index)
        _play_track(window, track_list, row_id)
    return {"ok": True}


def api_search(query: str) -> dict:
    with UIA_LOCK, uiautomation.UIAutomationInitializerInThread():
        window = _window()
        section = _do_search(window, query)
        items = []
        for row in (_search_rows(section) if section else []):
            kids = row.GetChildren()
            grid = kids[0] if kids else None
            texts = [t.Name for t in (grid.GetChildren() if grid else [])
                     if t.ControlTypeName == "TextControl" and t.Name]
            if texts:
                items.append({"t": texts[0],
                              "a": texts[1] if len(texts) > 1 else "",
                              "id": row.Name})
    return {"ok": True, "items": items}


def api_search_play(query: str, index: int, row_id: str) -> dict:
    """Re-run the search (results are deterministic) and play the chosen row.

    Row names are not unique across results, so the row is matched by
    position first and by name as a fallback.
    """
    with UIA_LOCK, uiautomation.UIAutomationInitializerInThread():
        window = _window()
        section = _do_search(window, query)
        rows = _search_rows(section) if section else []
        row = None
        if 0 <= index < len(rows) and rows[index].Name == row_id:
            row = rows[index]
        else:
            row = next((r for r in rows if r.Name == row_id), None)
        if not row:
            raise RuntimeError("search result not found")
        _invoke_row_play(window, row)
    return {"ok": True}


def _queue_list(window, timeout: float = 3):
    """Find the current queue list, including the older autoplay variant."""
    deadline = time.monotonic() + timeout
    while True:
        for aid in ("PlayQueueListView", "PlayQueueListViewAutoplay"):
            queue_list = uiautomation.ListControl(searchFromControl=window,
                                                  AutomationId=aid)
            if queue_list.Exists(0):
                return queue_list
        if time.monotonic() >= deadline:
            raise RuntimeError("queue list not found (PlayQueueListView or PlayQueueListViewAutoplay)")
        time.sleep(0.1)


def _queue_panel_state(window) -> int:
    toggle = _transport_button(window, "PlayQueueToggleButton").GetPattern(10015)
    if not toggle:
        raise RuntimeError("queue button does not support TogglePattern")
    state = toggle.ToggleState
    if state not in (0, 1):
        raise RuntimeError("queue panel state is indeterminate")
    return state


def _set_queue_panel(window, state: int) -> None:
    if _queue_panel_state(window) == state:
        return
    _transport_button(window, "PlayQueueToggleButton").GetTogglePattern().Toggle()
    deadline = time.monotonic() + 3
    while _queue_panel_state(window) != state:
        if time.monotonic() >= deadline:
            raise RuntimeError("queue panel did not reach requested state")
        time.sleep(0.1)


class QueueChangedError(RuntimeError):
    pass


def _queue_entries(queue_list) -> list:
    entries = []
    for row in queue_list.GetChildren():
        grid = row.GetChildren()
        texts = [t.Name for t in (grid[0].GetChildren() if grid else [])
                 if t.ControlTypeName == "TextControl" and t.Name.strip()
                 and not ICON_GLYPH_RE.fullmatch(t.Name.strip())]
        if texts:
            duration = texts[-1] if TRACK_DURATION_RE.match(texts[-1]) else ""
            entries.append((row, {"t": texts[0],
                                  "a": texts[1] if len(texts) > (2 if duration else 1) else "",
                                  "d": duration}))
    return entries


def _queue_revision(entries) -> str:
    # Include order and duplicates; never trust a stale positional index.
    data = [(row.Name, item) for row, item in entries]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _activate_queue_row(window, row) -> None:
    invoke = row.GetPattern(10000)
    if invoke:
        invoke.Invoke()
        return
    # WinUI queue rows advertise a legacy default action which silently does
    # nothing in current Apple Music. Activate the actual row by double click.
    window.SetActive(waitTime=0.15)
    scroll = row.GetPattern(10017)
    if scroll:
        scroll.ScrollIntoView()
        time.sleep(0.15)
    rect = row.BoundingRectangle
    if row.IsOffscreen or rect.width() <= 0 or rect.height() <= 0:
        raise RuntimeError("歌曲行不可见，请在电脑上打开 Apple Music 的队列后重试")
    # Do not click through another app or a dialog if foreground activation failed.
    target = uiautomation.ControlFromPoint((rect.left + rect.right) // 2,
                                          (rect.top + rect.bottom) // 2)
    row_id = row.GetRuntimeId()
    for _ in range(8):
        if not target:
            break
        if target.GetRuntimeId() == row_id:
            row.DoubleClick(simulateMove=False, waitTime=0.1)
            return
        target = target.GetParentControl()
    raise RuntimeError("Apple Music 的歌曲行被遮挡，请在电脑上打开应用后重试")


async def _queue_playback_snapshot():
    session = await _session()
    if not session:
        return None
    props = await session.try_get_media_properties_async()
    timeline = session.get_timeline_properties()
    artist, album = lyric_metadata(props.artist, props.album_title)
    return dict(title=props.title, artist=artist, album=album,
                duration=timeline.end_time.total_seconds(),
                position=timeline.position.total_seconds(),
                playing=session.get_playback_info().playback_status == PlaybackStatus.PLAYING)


def _track_matches(snapshot, title, artist='', album='', duration=None):
    """SMTC snapshot is this recording: title, artist, album (when SMTC has one)
    and duration within 2 s. A zero duration (still connecting) never matches."""
    if not snapshot or _lyric_normalize(snapshot['title']) != _lyric_normalize(title):
        return False
    if artist and _lyric_normalize(snapshot['artist']) != _lyric_normalize(artist):
        return False
    if album and snapshot.get('album') and _lyric_normalize(snapshot['album']) != _lyric_normalize(album):
        return False
    if duration is not None and abs(snapshot['duration'] - duration) > 2:
        return False
    return True


def _queue_track_matches(snapshot, item):
    artist, album = lyric_metadata(item.get('a', ''), '')
    duration = item.get('d', '')
    seconds = None
    if TRACK_DURATION_RE.fullmatch(duration):
        minutes, rest = map(int, duration.split(':'))
        seconds = minutes * 60 + rest
    return _track_matches(snapshot, item['t'], artist, album, seconds)


async def _confirm_playback(matches, before=None, timeout=5, transition=None) -> dict:
    """Wait until SMTC reports a matching item as playing.

    Already playing the same recording is not evidence of a successful
    activation: require a transition (or ``transition(current)``) or a
    position reset relative to ``before``.
    """
    async def wait_for_track():
        changed = not matches(before) or not before['playing']
        while True:
            current = await _queue_playback_snapshot()
            if current:
                matched = matches(current)
                if not matched or not current['playing'] or (transition and transition(current)):
                    changed = True
                restarted = bool(before and current['position'] + 1 < before['position'])
                if matched and current['playing'] and (changed or restarted):
                    return current
            await asyncio.sleep(0.2)
    try:
        return await asyncio.wait_for(wait_for_track(), timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError("尚未确认切换成功，请检查 Apple Music 当前歌曲后重试") from None


async def _confirm_queue_play(item: dict, before=None, timeout=5) -> None:
    await _confirm_playback(lambda snapshot: _queue_track_matches(snapshot, item), before, timeout)


def play_queue_item(index: int, revision: str) -> dict:
    with UIA_LOCK, uiautomation.UIAutomationInitializerInThread():
        window = _window()
        initial_state = _queue_panel_state(window)
        result = {"ok": True}
        try:
            _set_queue_panel(window, 1)
            entries = _queue_entries(_queue_list(window))
            if (_queue_revision(entries) != revision or not 0 <= index < len(entries)):
                raise QueueChangedError("待播队列已变化，已刷新列表，请重新选择歌曲")
            row = entries[index][0]
            before = asyncio.run(asyncio.wait_for(_queue_playback_snapshot(), timeout=2))
            _activate_queue_row(window, row)
            asyncio.run(_confirm_queue_play(entries[index][1], before))
        finally:
            try:
                _set_queue_panel(window, initial_state)
            except Exception as exc:
                logging.exception("Could not restore Apple Music queue panel after selection")
                result["restore_err"] = str(exc)
        return result


def api_queue() -> dict:
    """Read the Up Next panel. Opens it briefly if it isn't already open."""
    with UIA_LOCK, uiautomation.UIAutomationInitializerInThread():
        window = _window()
        initial_state = _queue_panel_state(window)
        read_error = restore_error = None
        try:
            if initial_state == 0:
                _set_queue_panel(window, 1)
            queue_list = _queue_list(window)
            entries = _queue_entries(queue_list)
            items = [item for _, item in entries]
            revision = _queue_revision(entries)
        except Exception as exc:
            read_error = exc
        finally:
            try:
                _set_queue_panel(window, initial_state)
            except Exception as exc:
                restore_error = exc
                logging.exception("Could not restore Apple Music queue panel")
        if read_error is not None:
            out = {"ok": False, "err": str(read_error)}
            if restore_error is not None:
                out["restore_err"] = str(restore_error)
            return out
        if restore_error is not None:
            return {"ok": False, "err": "queue read succeeded, but panel restore failed",
                    "restore_err": str(restore_error)}
    return {"ok": True, "items": items, "revision": revision}


# --------------------------------------------------------------------------
# Catalog playback (search results, album and artist pages)
#
# A music:// link opens the album in Apple Music 1.1792 and brings its main
# window forward, but never starts playback. The song is then started like a
# user would: its row's 更多 menu → 播放“title” (pure UIA), falling back to a
# hit-tested double click on the title. Row InvokePattern does nothing.
# Rows show store metadata (iTunes Lookup language), not the page language.
# --------------------------------------------------------------------------

ALBUM_HEADER_AUTOMATION_ID = "AlbumDetailHeaderLockupContent"
ICON_GLYPH_RE = re.compile(r"^[-\U000f0000-\U0010ffff]+$")
CATALOG_ID_RE = re.compile(r"[1-9][0-9]{0,14}")
CATALOG_CONFIRM_TIMEOUT = 8  # SMTC shows "正在连接…" with no duration for a moment


def _open_app_link(url: str) -> None:
    os.startfile(url)


def _album_header(window, title: str, timeout: float = 10):
    """Wait for the album page whose header title matches (then let rows settle)."""
    expected = _lyric_normalize(title)
    deadline = time.monotonic() + timeout
    while True:
        try:
            header = uiautomation.GroupControl(searchFromControl=window, searchDepth=12,
                                               AutomationId=ALBUM_HEADER_AUTOMATION_ID)
            if header.Exists(0, 0):
                texts = [c.Name for c in header.GetChildren() if c.ControlTypeName == "TextControl" and c.Name]
                if texts and _lyric_normalize(texts[0]) == expected:
                    time.sleep(0.4)
                    return uiautomation.GroupControl(searchFromControl=window, searchDepth=12,
                                                     AutomationId=ALBUM_HEADER_AUTOMATION_ID)
        except Exception:
            pass  # the page is being replaced while we read it
        if time.monotonic() >= deadline:
            raise RuntimeError("Apple Music 没有打开这张专辑，请确认电脑已解锁后重试")
        time.sleep(0.2)


def _album_row_info(row) -> dict:
    """Title, m:ss duration and track number of an album row.

    Children: favorite button, number (omitted while that row plays), title,
    optional explicit glyph and featured artists, duration, 更多 button.
    """
    kids = row.GetChildren()
    texts = [t.Name for t in (kids[0].GetChildren() if kids else [])
             if t.ControlTypeName == "TextControl" and t.Name and not ICON_GLYPH_RE.match(t.Name)]
    number = re.match(r"\D*?(\d+)\s", row.Name or "")
    number = int(number.group(1)) if number else None
    if texts and number is not None and texts[0] == str(number):
        texts = texts[1:]
    duration = next((t for t in reversed(texts) if TRACK_DURATION_RE.match(t)), "")
    title = next((t for t in texts if t != duration), "")
    return {"title": title, "duration": duration, "number": number}


def _find_album_row(header, track: dict):
    section = header.GetParentControl()
    track_list = section.GetParentControl() if section else None
    if not track_list or track_list.ControlTypeName != "ListControl":
        raise RuntimeError("找不到 Apple Music 专辑页的曲目列表")
    title = _lyric_normalize(track["title"])
    for rows in _walk_rows(track_list):
        matches = []
        for row in rows:
            if row.ControlTypeName != "ListItemControl":
                continue
            info = _album_row_info(row)
            if _lyric_normalize(info["title"]) != title:
                continue
            if info["duration"] and track.get("duration"):
                minutes, seconds = map(int, info["duration"].split(":"))
                if abs(minutes * 60 + seconds - track["duration"]) > 2:
                    continue
            matches.append((row, info))
        if len(matches) > 1 and track.get("number"):
            matches = [m for m in matches if m[1]["number"] == track["number"]] or matches
        if len(matches) == 1:
            return matches[0][0]
        if matches:
            raise RuntimeError("专辑中有多首同名歌曲，请在电脑上选择")
    raise RuntimeError("在 Apple Music 专辑页中找不到这首歌")


def _album_row_menu_item(window, title: str):
    menu = uiautomation.MenuControl(searchFromControl=window, ClassName="MenuFlyout", searchDepth=8)
    if not menu.Exists(0, 0):
        return None
    items = [i for i in menu.GetChildren()
             if i.ControlTypeName == "MenuItemControl" and i.ClassName == "MenuFlyoutItem"]
    quoted = (f"“{title}”", f'"{title}"', f"「{title}」")
    for item in items:
        if any(q in (item.Name or "") for q in quoted):
            return item
    wanted = _lyric_normalize(title)[:10]
    return next((i for i in items if wanted and (i.Name or "").startswith(("播放", "Play"))
                 and wanted in _lyric_normalize(i.Name)), None)


def _close_menu(window) -> None:
    if uiautomation.MenuControl(searchFromControl=window, ClassName="MenuFlyout", searchDepth=8).Exists(0, 0):
        window.SetActive(waitTime=0.1)
        uiautomation.SendKeys("{Esc}", waitTime=0.2)


def _double_click_album_row(window, row, title: str) -> None:
    window.SetActive(waitTime=0.15)
    kids = row.GetChildren()
    texts = [t for t in (kids[0].GetChildren() if kids else []) if t.ControlTypeName == "TextControl"]
    target = next((t for t in texts if _lyric_normalize(t.Name) == _lyric_normalize(title)), row)
    rect = target.BoundingRectangle
    if row.IsOffscreen or rect.width() <= 0 or rect.height() <= 0:
        raise RuntimeError("歌曲行不可见，请在电脑上打开 Apple Music 后重试")
    # Click the title text only when it is really on top; never a link or button.
    hit = uiautomation.ControlFromPoint((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
    row_id = row.GetRuntimeId()
    for _ in range(8):
        if not hit or hit.ControlTypeName in ("ButtonControl", "HyperlinkControl"):
            break
        if hit.GetRuntimeId() == row_id:
            target.DoubleClick(simulateMove=False, waitTime=0.1)
            return
        hit = hit.GetParentControl()
    raise RuntimeError("Apple Music 的歌曲行被遮挡，请在电脑上打开应用后重试")


def _play_album_row(window, row, title: str) -> str:
    try:
        row.GetScrollItemPattern().ScrollIntoView()
        time.sleep(0.2)
    except Exception:
        pass
    kids = row.GetChildren()
    buttons = [b for b in (kids[0].GetChildren() if kids else [])
               if b.ControlTypeName == "ButtonControl" and b.AutomationId != "FavoriteButton"]
    if buttons:
        buttons[-1].GetInvokePattern().Invoke()
        deadline = time.monotonic() + 3.5
        while time.monotonic() < deadline:
            item = _album_row_menu_item(window, title)
            if item:
                item.GetInvokePattern().Invoke()
                return "menu"
            time.sleep(0.15)
        _close_menu(window)
    _double_click_album_row(window, row, title)
    return "double_click"


def _shuffle_on(window) -> bool:
    try:
        return _transport_button(window, "ShuffleButton").GetTogglePattern().ToggleState == 1
    except Exception:
        return False


def play_catalog_item(kind: str, album_id: str, song_id: Optional[str] = None, shuffle: bool = False) -> dict:
    """Play a catalog song (queue becomes its album from that song) or album.

    Brings Apple Music's main window forward. Volume is never touched.
    """
    meta = apple_catalog.app_metadata(album_id)  # network, outside UIA_LOCK
    track = meta["tracks"].get(song_id) if kind == "song" else None
    if kind == "song" and not track:
        raise RuntimeError("这首歌不在该专辑中，请刷新后重试")
    link = apple_catalog.app_link("album", album_id, song_id if kind == "song" else None)
    with UIA_LOCK, uiautomation.UIAutomationInitializerInThread():
        _window()  # running app only; a link would otherwise launch it slowly
        before = asyncio.run(asyncio.wait_for(_queue_playback_snapshot(), timeout=2))
        _open_app_link(link)
        time.sleep(0.3)
        window = _window()
        header = _album_header(window, meta["title"])
        if kind == "song":
            row = _find_album_row(header, track)
            method = _play_album_row(window, row, track["title"])
            current = asyncio.run(_confirm_playback(
                lambda s: _track_matches(s, track["title"], track["artist"], meta["title"], track["duration"]),
                before, timeout=CATALOG_CONFIRM_TIMEOUT))
        else:
            method = "shuffle_button" if shuffle else "play_button"
            button = uiautomation.ButtonControl(searchFromControl=header, searchDepth=2,
                                                AutomationId="ShuffleButtonElement" if shuffle else "PlayButtonElement")
            if not button.Exists(1):
                raise RuntimeError("专辑页没有可用的播放按钮")
            shuffle_before = _shuffle_on(window)
            button.GetInvokePattern().Invoke()
            album = _lyric_normalize(meta["title"])

            def matches(s):
                return bool(s) and _lyric_normalize(s.get("album") or "") == album and (not shuffle or _shuffle_on(window))

            def transition(s):
                # Shuffling the album that already plays only turns shuffle on.
                return (not before or s["title"] != before["title"]
                        or (shuffle and not shuffle_before and _shuffle_on(window)))
            current = asyncio.run(_confirm_playback(matches, before, timeout=CATALOG_CONFIRM_TIMEOUT,
                                                     transition=transition))
    return {"ok": True, "method": method, "title": current["title"], "artist": current["artist"]}


def open_catalog_page(kind: str, item_id: str) -> dict:
    """Only show an album or artist page in Apple Music on the computer."""
    link = apple_catalog.app_link(kind, item_id)
    with UIA_LOCK:
        _open_app_link(link)
    return {"ok": True}


def catalog_request(route: str, body) -> dict:
    """Strict JSON validation for /catalog/play and /catalog/open."""
    def valid_id(value):
        return isinstance(value, str) and CATALOG_ID_RE.fullmatch(value) is not None
    if isinstance(body, dict) and route == "/catalog/play":
        if body.get("kind") == "song" and set(body) == {"kind", "album_id", "song_id"} \
                and valid_id(body["album_id"]) and valid_id(body["song_id"]):
            return {"kind": "song", "album_id": body["album_id"], "song_id": body["song_id"]}
        if body.get("kind") == "album" and set(body) == {"kind", "album_id", "shuffle"} \
                and valid_id(body["album_id"]) and type(body["shuffle"]) is bool:
            return {"kind": "album", "album_id": body["album_id"], "shuffle": body["shuffle"]}
    if isinstance(body, dict) and route == "/catalog/open":
        if set(body) == {"kind", "id"} and body["kind"] in ("album", "artist") and valid_id(body["id"]):
            return {"kind": body["kind"], "item_id": body["id"]}
    raise ValueError("Invalid catalog request")


def api_catalog_resolve(query: dict) -> dict:
    """Match the current SMTC song; edition markers are read before stripping."""
    title, raw_artist, raw_album = (query.get(f, [""])[0] for f in ("title", "artist", "album"))
    try:
        duration = float(query.get("duration", [""])[0])
    except (TypeError, ValueError):
        raise apple_catalog.CatalogError("invalid") from None
    marker = re.search(r"\[(explicit|clean)\]", raw_artist + " " + raw_album, re.I)
    artist, album = lyric_metadata(raw_artist, raw_album)
    return apple_catalog.resolve(title, artist, album, duration, marker.group(1).lower() if marker else "")


# --------------------------------------------------------------------------
# Third-party lyrics (LDDC multi-source); independent of SMTC, COM and UIA.
# --------------------------------------------------------------------------

LRCLIB_URL = "https://lrclib.net/api/"
LRCLIB_AGENT = "AppleMusicRemote/1.2.0 (https://github.com/ahmetberber/windows-apple-music-remote)"
LYRICS_BUDGET = 18.0
LYRIC_ALIAS_BUDGET = 4.0
LYRIC_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]")
LYRICS_CACHE_LIMIT = 256
LYRICS_LOCK = threading.Lock()
LYRICS_SOURCE_LOCK = threading.Lock()
LYRICS_WORKER = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lyrics")
LYRICS_CACHE = OrderedDict()
LYRICS_PENDING = {}
LYRICS_NEXT_REQUEST = 0.0
LYRICS_RETRY_AT = 0.0


def lyric_metadata(artist: str, album: str) -> tuple[str, str]:
    # Windows Apple Music sometimes reports "artist — album [Explicit]" with
    # an empty album_title. Preserve the display metadata, split only this
    # spaced em-dash format for lyrics, never ordinary hyphens in artist names.
    artist, album = (artist or "").strip(), (album or "").strip()
    if " — " in artist:
        name, suffix = artist.rsplit(" — ", 1)
        if name and suffix and (not album or _lyric_normalize(suffix) == _lyric_normalize(album)):
            artist, album = name, album or suffix
    album = re.sub(r"\s*\[(?:explicit|clean)\]\s*$", "", album, flags=re.I).strip()
    return artist, album


def _lyric_normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join("".join(c if c.isalnum() else " " for c in value).split())


def select_lyric_record(records: list, track: dict, approximate: bool = False) -> Optional[dict]:
    """Recording match with the LDDC sources' rules, not the first search hit.

    Exact: the same core title (feat./theme-song/remaster suffixes removed),
    an overlapping artist (also via store-localized aliases), duration within
    four seconds and the same version markers. Approximate: the same core
    title within two seconds, any artist. Album only ranks; lyrics against an
    instrumental record with near-identical scores is rejected as ambiguous.
    Never imports LDDC (lyrics_matching's non-fuzzy paths).
    """
    ranked = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if not all(isinstance(record.get(k) or "", str) for k in ("trackName", "artistName", "albumName")):
            continue
        score = approximate_score(record, track) if approximate else score_record(record, track, fuzzy=False)
        if score is not None:
            ranked.append((score, bool(record.get("syncedLyrics")), record))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if not ranked:
        return None
    best = ranked[0][2]
    for score, _, other in ranked[1:]:
        if ranked[0][0] - score < 4 and bool(best.get("instrumental")) != bool(other.get("instrumental")):
            return None
    return best


def parse_lrc(text: str) -> list[dict]:
    """Parse repeated timestamps, milliseconds, global offset and blank breaks.

    Group simultaneous lines (e.g. original + translation) for highlighting.
    """
    offset_match = re.search(r"\[offset\s*:\s*([+-]?\d+)\]", text, re.I)
    offset = int(offset_match.group(1)) / 1000 if offset_match else 0
    stamp = re.compile(r"\[(\d{1,3}):([0-5]\d)(?:[.:](\d{1,3}))?\]")
    grouped = {}
    for raw in text.splitlines():
        matches = list(stamp.finditer(raw))
        if not matches:
            continue
        content = stamp.sub("", raw).strip()
        for match in matches:
            fraction = float("0." + match.group(3)) if match.group(3) else 0
            seconds = max(0, round(int(match.group(1)) * 60 + int(match.group(2)) + fraction + offset, 3))
            parts = grouped.setdefault(seconds, [])
            if content not in parts:
                parts.append(content)
    return [{"time": seconds, "text": "\n".join(parts)} for seconds, parts in sorted(grouped.items())]


def _lyric_error(reason: str) -> dict:
    return {"ok": False, "state": "error", "source": "LRCLIB", "error": reason}


def _lrclib_request(endpoint: str, params: dict, deadline: float):
    # Multiple song jobs share the legacy source's throttle/backoff state.
    if not LYRICS_SOURCE_LOCK.acquire(timeout=max(0, deadline - time.monotonic())):
        raise TimeoutError('timeout')
    try:
        return _lrclib_request_unlocked(endpoint, params, deadline)
    finally:
        LYRICS_SOURCE_LOCK.release()


def _lrclib_request_unlocked(endpoint: str, params: dict, deadline: float):
    global LYRICS_NEXT_REQUEST, LYRICS_RETRY_AT
    now = time.monotonic()
    if now < LYRICS_RETRY_AT:
        raise RuntimeError("rate_limited")
    delay = max(0, LYRICS_NEXT_REQUEST - now)
    if deadline - now <= delay:
        raise TimeoutError("timeout")
    if delay:
        time.sleep(delay)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("timeout")
    request = Request(LRCLIB_URL + endpoint + "?" + urlencode(params),
                      headers={"User-Agent": LRCLIB_AGENT, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=min(4.0, remaining)) as response:
            if "application/json" not in response.headers.get("Content-Type", ""):
                raise ValueError("invalid_response")
            # Bound both response size and time spent reading a slow response.
            chunks, size = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("timeout")
                chunk = response.read1(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > 2_000_000:
                    raise ValueError("response_too_large")
                chunks.append(chunk)
            return json.loads(b"".join(chunks))
    except HTTPError as exc:
        if exc.code == 404:
            return None
        if exc.code in (429, 503):
            retry = exc.headers.get("Retry-After", "60" if exc.code == 429 else "1")
            try:
                seconds = float(retry)
            except ValueError:
                try:
                    seconds = parsedate_to_datetime(retry).timestamp() - time.time()
                except (TypeError, ValueError, OverflowError):
                    seconds = 60
            if not math.isfinite(seconds):
                seconds = 60
            LYRICS_RETRY_AT = time.monotonic() + max(1, seconds)
            raise RuntimeError("rate_limited" if exc.code == 429 else "source_busy") from exc
        raise
    finally:
        LYRICS_NEXT_REQUEST = time.monotonic() + .25


def _fetch_lrclib_lyrics(track: dict, deadline: float) -> dict:
    try:
        params = {"track_name": track["title"], "artist_name": track["artist"]}
        if track["album"]:
            params["album_name"] = track["album"]
        # The current API validates duration <= 3600; search still lets us
        # verify a longer recording locally without submitting invalid get data.
        if track["duration"] <= 3600:
            params["duration"] = track["duration"]
        direct = _lrclib_request("get", params, deadline)
        record = select_lyric_record([direct], track) if direct else None
        candidates = None
        if not record:
            candidates = _lrclib_request("search", {"track_name": track["title"],
                                                     "artist_name": track["artist"]}, deadline)
            if candidates is not None and not isinstance(candidates, list):
                raise ValueError("invalid_response")
            record = select_lyric_record(candidates or [], track)
        result = {"ok": True, "state": "not_found", "source": "LRCLIB", "lines": [], "plain": ""}
        if not record:
            # No exact match in either response: same core title and duration, any artist.
            record = select_lyric_record(([direct] if direct else []) + (candidates or []), track, approximate=True)
            if not record:
                return result
            result["match_quality"] = "approximate"
        result["record"] = {key: record.get(key) for key in
                            ("id", "trackName", "artistName", "albumName", "duration")}
        if record.get("instrumental"):
            result["state"] = "instrumental"
        else:
            synced, plain = record.get("syncedLyrics") or "", record.get("plainLyrics") or ""
            result["lines"] = parse_lrc(synced)
            # Malformed/untimed LRC still falls back to readable plain lyrics.
            if result["lines"] and any(line["text"] for line in result["lines"]):
                result["state"] = "synced"
            elif plain.strip():
                result.update(state="plain", plain=plain, lines=[])
        return translation_service().register(result, record.get("syncedLyrics") or "",
                                               record.get("plainLyrics") or "")
    except (TimeoutError, socket.timeout):
        return _lyric_error("timeout")
    except Exception as exc:
        logging.info("LRCLIB request failed: %s", type(exc).__name__)
        return _lyric_error(str(exc) if isinstance(exc, RuntimeError) else "source_unavailable")


def _fetch_lyrics(track: dict, deadline: float, candidate: str = 'auto') -> dict:
    try:
        result = multisource_lyrics.load(track, deadline, _fetch_lrclib_lyrics, candidate)
        # Hash actual normalized content AND preserved word timings. LDDC's
        # native translation tracks are retained but never guessed into rows.
        return translation_service().register(result, '', result.get('plain', ''))
    except Exception:
        return dict(ok=False, state='error', source='LDDC', error='processing_failed')


def _cache_lyrics(key: tuple, future) -> None:
    result = future.result()
    ttl = 30 if result["state"] == "error" else 6 * 3600 if result["state"] == "not_found" else 24 * 3600
    with LYRICS_LOCK:
        LYRICS_PENDING.pop(key, None)
        LYRICS_CACHE[key] = (time.monotonic() + ttl, result)
        LYRICS_CACHE.move_to_end(key)
        while len(LYRICS_CACHE) > LYRICS_CACHE_LIMIT:
            LYRICS_CACHE.popitem(last=False)


def _lyric_aliases(track: dict, deadline: float) -> list:
    """The same song's names in Apple's tw/hk catalogs (Mayday → 五月天,
    So Be It → 最好是) as extra lyric search/match names; [] when unavailable.

    The resolve step shares the cache of the page's /catalog/resolve.
    """
    if LYRIC_CJK_RE.search(track["title"]) and LYRIC_CJK_RE.search(track["artist"]):
        return []
    limit = min(deadline, time.monotonic() + LYRIC_ALIAS_BUDGET)
    try:
        match = apple_catalog.resolve(track["title"], track["artist"], track["album"], track["duration"],
                                      deadline=limit).get("match")
        local = apple_catalog.localized(match["id"], deadline=limit) if match else None
    except apple_catalog.CatalogError:
        return []
    if not local or ((_lyric_normalize(local["title"]), _lyric_normalize(local["artist"]))
                     == (_lyric_normalize(track["title"]), _lyric_normalize(track["artist"]))):
        return []
    return [{field: local[field] for field in ("title", "artist", "album")}]


def api_lyrics(query: dict) -> dict:
    deadline = time.monotonic() + LYRICS_BUDGET
    track = {field: query.get(field, [""])[0].strip() for field in ("title", "artist", "album")}
    try:
        track["duration"] = float(query.get("duration", ["0"])[0])
    except (ValueError, TypeError):
        return _lyric_error("invalid_metadata")
    if (not track["title"] or not track["artist"] or not math.isfinite(track["duration"])
            or not 1 <= track["duration"] <= 86400 or any(len(track[f]) > 500 for f in ("title", "artist", "album"))):
        return {"ok": True, "state": "not_found", "source": "LDDC 多源", "reason": "insufficient_metadata"}
    candidate = query.get('candidate', ['auto'])[0]
    if candidate != 'auto' and not re.fullmatch(r'(QM|NE|KG|LRCLIB)-[a-f0-9]{24}', candidate):
        return dict(ok=False, state='error', source='LDDC', error='invalid_candidate')
    aliases = _lyric_aliases(track, deadline)
    if aliases:
        track["aliases"] = aliases
    # Aliases are part of the key, so a result cached while the catalog lookup
    # failed does not hide a later lookup that succeeds.
    key = (tuple(_lyric_normalize(track[f]) for f in ("title", "artist", "album")) + (round(track["duration"], 3), candidate)
           + tuple((_lyric_normalize(a["title"]), _lyric_normalize(a["artist"])) for a in aliases))
    created = False
    with LYRICS_LOCK:
        entry = LYRICS_CACHE.get(key)
        if entry and entry[0] > time.monotonic() and translation_service().touch(entry[1]):
            LYRICS_CACHE.move_to_end(key)
            return dict(entry[1], cached=True)
        future = LYRICS_PENDING.get(key)
        if future is None:
            if len(LYRICS_PENDING) >= 8:
                return _lyric_error("busy")
            future = LYRICS_WORKER.submit(_fetch_lyrics, track, deadline, candidate)
            LYRICS_PENDING[key] = future
            created = True
    # Register outside the lock: already-completed futures invoke immediately.
    if created:
        future.add_done_callback(lambda done: _cache_lyrics(key, done))
    try:
        return dict(future.result(timeout=max(.01, deadline - time.monotonic())), cached=False)
    except FutureTimeout:
        return _lyric_error("timeout")


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

def api_status():
    try:
        state = asyncio.run(_status())
    except Exception as exc:
        state = dict(ok=False, err=str(exc))
    return dict(state, lyric_settings=LYRIC_SETTINGS.snapshot())


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        path = urlparse(self.path).path
        if (path in ("/", "/status", "/queue", "/modes", "/lyrics", "/lyrics/translation", "/settings/lyrics")
                or path.startswith("/catalog/")):
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except ConnectionError:
            pass  # a client can cancel its old-song lyrics fetch during a skip

    def _json(self, fn) -> None:
        try:
            out = fn()
        except Exception as exc:
            out = {"ok": False, "err": str(exc)}
        self._send(200, json.dumps(out).encode(), "application/json")

    def do_GET(self) -> None:
        if self.path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path.startswith("/status"):
            self._json(api_status)
        elif urlparse(self.path).path == '/settings/lyrics':
            self._json(LYRIC_SETTINGS.snapshot)
        elif urlparse(self.path).path == "/lyrics":
            self._json(lambda: api_lyrics(parse_qs(urlparse(self.path).query)))
        elif urlparse(self.path).path == "/lyrics/translation":
            query = parse_qs(urlparse(self.path).query)
            self._json(lambda: translation_service().snapshot(query.get("lyrics_id", [""])[0]))
        elif urlparse(self.path).path.startswith("/catalog/"):
            self._catalog_get()
        elif (urlparse(self.path).path in ("/playlists", "/search")
              or urlparse(self.path).path.startswith("/tracks/")):
            self._send(410, b'{"ok":false,"err":"Feature removed"}', "application/json")
        elif self.path.startswith("/modes"):
            self._json(api_modes)
        elif urlparse(self.path).path == "/queue":
            self._json(api_queue)
        elif self.path.startswith("/artwork"):
            try:
                data = asyncio.run(_artwork())
            except OSError:  # thumbnail stream can vanish during track changes
                data = None
            self._send(200, data, "image/jpeg") if data else self._send(404)
        else:
            self._send(404)

    def _catalog_get(self) -> None:
        # Catalog data is cached server-side (search 15 min, items 24 h,
        # failures 60 s); responses themselves are no-store.
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        item = re.fullmatch(r"/catalog/(album|artist)/([0-9]{1,15})", parsed.path)
        try:
            if parsed.path == "/catalog/search":
                result = apple_catalog.search(query.get("term", [""])[0], query.get("kind", ["song"])[0])
            elif parsed.path == "/catalog/resolve":
                result = api_catalog_resolve(query)
            elif item:
                result = (apple_catalog.album if item.group(1) == "album" else apple_catalog.artist)(item.group(2))
            else:
                return self._send(404)
        except apple_catalog.CatalogError as exc:
            return self._send(exc.status, json.dumps(exc.payload()).encode(), "application/json")
        except Exception:
            logging.exception("Catalog request failed")
            error = apple_catalog.CatalogError("unavailable")
            return self._send(error.status, json.dumps(error.payload()).encode(), "application/json")
        self._send(200, json.dumps(result).encode(), "application/json")

    def _catalog_post(self, route: str) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 256 or self.headers.get_content_type() != "application/json":
                raise ValueError("Invalid request")
            self.connection.settimeout(5)
            request = catalog_request(route, json.loads(self.rfile.read(length)))
        except (ValueError, OSError):
            return self._send(400, json.dumps(apple_catalog.CatalogError("invalid").payload()).encode(),
                              "application/json")
        try:
            result = play_catalog_item(**request) if route == "/catalog/play" else open_catalog_page(**request)
        except apple_catalog.CatalogError as exc:
            return self._send(exc.status, json.dumps(exc.payload()).encode(), "application/json")
        except Exception as exc:
            logging.warning("Catalog %s failed: %s", route, exc)
            return self._send(503, json.dumps({"ok": False, "err": str(exc) or "Apple Music 播放失败，请重试"}).encode(),
                              "application/json")
        self._send(200, json.dumps(result).encode(), "application/json")

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_POST(self) -> None:
        # Apply the same policy to ALL writes, including bodyless /cmd routes.
        # Native clients without Origin remain supported; browser cross-site
        # requests and opaque ("null") origins must never control this computer.
        origin = self.headers.get('Origin')
        try:
            source = urlparse(origin) if origin is not None else None
            target = urlparse('http://' + (self.headers.get('Host') or ''))
            if ((source is not None and
                 (source.scheme != 'http' or not source.hostname or source.username or source.password
                  or source.path not in ('', '/') or source.query or source.fragment
                  or source.hostname != target.hostname or (source.port or 80) != (target.port or 80)))
                    or (source is None and self.headers.get('Sec-Fetch-Site') == 'cross-site')):
                return self._send(403)
        except ValueError:
            return self._send(403)
        if urlparse(self.path).path in ('/catalog/play', '/catalog/open'):
            return self._catalog_post(urlparse(self.path).path)
        if urlparse(self.path).path == '/queue/play':
            try:
                length = int(self.headers.get('Content-Length') or 0)
                if not 0 < length <= 256 or self.headers.get_content_type() != 'application/json':
                    raise ValueError('Invalid request')
                self.connection.settimeout(5)
                body = json.loads(self.rfile.read(length))
                if (not isinstance(body, dict) or set(body) != {'index', 'revision'}
                        or type(body['index']) is not int or body['index'] < 0
                        or not isinstance(body['revision'], str)
                        or not re.fullmatch(r'[0-9a-f]{64}', body['revision'])):
                    raise ValueError('Invalid selection')
            except (ValueError, OSError):
                return self._send(400, b'{"ok":false,"err":"Invalid queue selection"}', 'application/json')
            try:
                result = play_queue_item(body['index'], body['revision'])
            except QueueChangedError as exc:
                return self._send(409, json.dumps({'ok': False, 'err': str(exc)}).encode(), 'application/json')
            except Exception as exc:
                return self._send(503, json.dumps({'ok': False, 'err': str(exc)}).encode(), 'application/json')
            return self._send(200, json.dumps(result).encode(), 'application/json')
        if urlparse(self.path).path == '/settings/lyrics':
            try:
                length = int(self.headers.get('Content-Length') or 0)
                if not 0 < length <= 256 or self.headers.get_content_type() != 'application/json':
                    raise ValueError('Invalid request')
                self.connection.settimeout(5)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or set(body) != {'lyric_offset', 'revision'}:
                    raise ValueError('Invalid settings')
            except (ValueError, OSError):
                return self._send(400, b'{"ok":false,"error":"invalid_settings"}', 'application/json')
            code, data = LYRIC_SETTINGS.update(body['lyric_offset'], body['revision'])
            return self._send(code, json.dumps(data).encode(), 'application/json')
        if urlparse(self.path).path == "/lyrics/translation":
            # Only small JSON commands referencing server-owned lyrics. Browsers
            # on other origins cannot turn this LAN service into a paid proxy.
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= 256 or self.headers.get_content_type() != "application/json":
                    return self._send(400)
                self.connection.settimeout(5)
                body = json.loads(self.rfile.read(length))
                if (not isinstance(body, dict) or set(body) - {"lyrics_id", "retry"}
                        or not isinstance(body.get("lyrics_id"), str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{32}", body["lyrics_id"])
                        or not isinstance(body.get("retry", False), bool)):
                    return self._send(400)
            except (ValueError, OSError):
                return self._send(400)
            return self._json(lambda: translation_service().start(body["lyrics_id"], body.get("retry", False)))
        if urlparse(self.path).path in ("/play", "/splay"):
            return self._send(410, b'{"ok":false,"err":"Feature removed"}', "application/json")

        parts = self.path.strip("/").split("/")  # cmd/<name>[/<arg>]
        if len(parts) < 2 or parts[0] != "cmd":
            return self._send(404)
        if len(parts) > 3:
            return self._send(404)
        command, arg = parts[1], parts[2] if len(parts) == 3 else None
        try:
            if command in ("toggle", "next", "prev") and arg is None:
                asyncio.run(_transport(command))
            elif command == "vol" and arg and arg.isascii() and arg.isdigit() and 0 <= int(arg) <= 100:
                set_volume(int(arg))
            elif command == "mute" and arg is None:
                toggle_mute()
            elif command == "seek" and arg and arg.isascii() and arg.isdigit():
                seek(int(arg))
            elif command in ("shuffle", "repeat") and arg is None:
                toggle_shuffle() if command == "shuffle" else cycle_repeat()
            else:
                return self._send(404)
        except Exception as exc:
            logging.warning("Playback command %s failed: %s", command, exc)
            return self._send(503, json.dumps({"ok": False, "err": str(exc)}).encode(), "application/json")
        self._send(204)

    def log_message(self, *args) -> None:
        pass  # keep the console quiet


def lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    finally:
        sock.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Web remote for Apple Music on Windows")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"HTTP port to listen on (default: {DEFAULT_PORT})")
    args = parser.parse_args()

    # Keep requests snappy when the Apple Music window is missing
    # (the default UIA search timeout is 10 seconds).
    uiautomation.SetGlobalSearchTimeout(2)

    print(f"Open on your phone:  http://{lan_ip()}:{args.port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

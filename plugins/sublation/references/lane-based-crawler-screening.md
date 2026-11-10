# Lane-Based Crawler Screening — 2026-06-23 Case Study

## Context

User asked to screen all crawler-class skills for sublation. Initial keyword-based scan returned 189 false positives (too broad). Narrowed to 12 installed skills + 5 desktop clones. Applied lane-based grouping before any merge proposal.

## Installed Skills (12)

### Browser / Anti-Bot Lane (keep distinct)
| Skill | Size | Role | Treatment |
|-------|------|------|-----------|
| scraping (Scrapling) | 4py/245KB | HTTP extraction, Cloudflare bypass | Keep as crawler framework |
| browser-harness | 16KB docs | Direct CDP control of user Chrome | Keep as attached-session control |
| agent-browser | 3KB | Official CLI stub | Exclude (.agents symlink) |
| obscura | 8KB docs | Rust headless browser + CDP | Keep as specialized engine |

**No merge.** Four different layers — HTTP extraction, user-attached CDP, CLI wrapper, Rust headless engine.

### Media Download Lane (donor boundary)
| Skill | Role | Treatment |
|-------|------|-----------|
| media-toolkit (18py/167KB) | Unified entry: yt-dlp + Douyin + compression | Main entry |
| universal-media-downloader (2py) | yt-dlp single URL | Donor/backend |
| douyin-batch-download (13py/123KB) | F2 Douyin creator workflow | Donor/backend |

**Evidence:** 3 scripts byte-identical (download_media.py, download-v2.py, batch-download.py). media-toolkit declares `absorbs`. Treatment: main entry + retained donors, no deletion.

### Social Capture Lane (supersession report first)
twitter-monitor (API key), canghe-danger-x-to-markdown (reverse-API consent), canghe-url-to-markdown (CDP wait-mode), wechat-article-fetch (Playwright). Different safety boundaries — no merge yet.

## Key Lesson

Group by functional layer before proposing merges. Tools at different layers are not interchangeable.

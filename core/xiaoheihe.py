"""
小黑盒解析（迁移自 rconsole-plugin/apps/tools.js 的 xiaoheihe 分支）

* 帖子：bbs/app/link/tree，正文可能是图文混排 HTML，也可能是一段 JSON 实体数组
* 游戏：pc / console / mobile 三种详情接口，输出评分、价格、社区评价、截图等

接口需要 hkey 签名与 Cookie，签名实现在 utils/xhh_sign.py。
"""

import html as html_lib
import json
import re
import urllib.parse
from typing import Any, AsyncGenerator, Dict, List, Optional

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..constants.tools import (
    COMMON_USER_AGENT,
    XHH_BBS_LINK,
    XHH_CONSOLE_LINK,
    XHH_GAME_LINK,
    XHH_MOBILE_LINK,
)
from ..utils.xhh_sign import get_api_params, optimize_image_url
from .common import (
    build_temp_path,
    chunked,
    create_forward_message,
    delete_file,
    download_file,
    fetch_json,
    get_message_text,
    local_image,
    release_video,
    truncate_str,
    video_components,
)

XHH_MSG_ELEMENT_LIMIT = 50
XHH_COMMENT_LIMIT = 50
MAX_COMMENTS = XHH_COMMENT_LIMIT
XHH_REFERER = "https://www.xiaoheihe.cn/"
GAME_API_MAP = {
    "pc": XHH_GAME_LINK,
    "console": XHH_CONSOLE_LINK,
    "mobile": XHH_MOBILE_LINK,
}


def _parse_target(msg: str):
    """从分享链接里判断类型和 ID"""
    if "bbs" in msg:
        match = re.search(r"bbs/link/([a-zA-Z0-9]+)", msg) or re.search(
            r"bbs/app/api/web/share\?[^\s]*?link_id=([a-zA-Z0-9]+)", msg
        )
        if match:
            return "bbs", match.group(1)
    for kind in ("pc", "console", "mobile"):
        if kind in msg:
            match = re.search(rf"game/{kind}/([a-zA-Z0-9]+)", msg) or re.search(
                rf"share_game_detail\?[^\s]*?appid=([a-zA-Z0-9]+)&game_type={kind}", msg
            )
            if match:
                return kind, match.group(1)
    return "", ""


def clean_html_text(raw: str) -> str:
    """把小黑盒正文 HTML 压成可读文本，并把 heybox:// 内部跳转还原成网页链接"""
    if not raw:
        return ""

    def _anchor(match: "re.Match") -> str:
        href = (match.group(1) or "").replace("\\", "")
        text = re.sub(r"<[^>]+>", "", match.group(2) or "").strip()
        if not text:
            return ""
        wrapped = f"『{text}』"
        try:
            decoded = urllib.parse.unquote(href)
            m = re.search(r"heybox://(\{.*\})", decoded)
            if m:
                data = json.loads(m.group(1))
                protocol = data.get("protocol_type")
                if protocol == "openUser" and data.get("user_id"):
                    return f"{wrapped} (https://www.xiaoheihe.cn/app/user/profile/{data['user_id']})"
                if protocol == "openGameDetail" and data.get("app_id"):
                    game_type = data.get("game_type") or "pc"
                    return f"{wrapped} (https://www.xiaoheihe.cn/app/topic/game/{game_type}/{data['app_id']})"
                link = data.get("link") or {}
                if protocol == "openLink" and link.get("linkid"):
                    return f"{wrapped} (https://www.xiaoheihe.cn/app/bbs/link/{link['linkid']})"
        except Exception:  # noqa: BLE001
            return wrapped
        if href.startswith("http"):
            return f"{wrapped} ({href})"
        return wrapped

    text = re.sub(r'<a[^>]*?href="([^"]*?)"[^>]*?>(.*?)</a>', _anchor, raw, flags=re.S)
    text = re.sub(r'<span[^>]*?data-emoji="([^"]*?)"[^>]*?>.*?</span>', lambda m: f"[{m.group(1)}]", text, flags=re.S)
    text = re.sub(r"</p>|</h[1-6]>|</blockquote>|<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html_lib.unescape(text).strip()


async def _api_get(url: str, params: Dict[str, Any], cookie: str) -> Optional[Any]:
    return await fetch_json(
        url,
        headers={"Cookie": cookie, "User-Agent": COMMON_USER_AGENT},
        params=params,
        timeout=20,
    )


async def _render_game_card(game_id: str, cookie: str) -> List[Any]:
    """正文里的游戏卡片"""
    components: List[Any] = []
    for kind, api in GAME_API_MAP.items():
        payload = await _api_get(api, get_api_params(kind, game_id), cookie)
        if not isinstance(payload, dict) or payload.get("status") != "ok" or not payload.get("result"):
            continue
        data = payload["result"]
        if data.get("image"):
            img = await local_image(optimize_image_url(data["image"]), referer=XHH_REFERER, prefix="xhh_card")
            if img is not None:
                components.append(img)

        lines: List[str] = []
        score_count = (data.get("comment_stats") or {}).get("score_comment") or 0
        if data.get("score"):
            lines.append(f"🌟 评分: {data['score']}" + (f" ({score_count}人)" if score_count > 0 else ""))
        else:
            lines.append("🌟 评分: 暂无评分" + (f" ({score_count}人)" if score_count > 0 else ""))

        price = data.get("price") or {}
        if price.get("current"):
            lines.append(f"💰 价格: ¥{price['current']}" + (f" (-{price['discount']}%)" if price.get("discount") else ""))
        elif (data.get("region_prices") or []):
            region = data["region_prices"][0]
            lines.append(
                f"💰 价格: {region.get('final_amount')}"
                + (f" (-{region.get('discount')}%)" if region.get("discount") else "")
                + f" ({region.get('region_name')})"
            )
        else:
            lines.append("💰 价格: 暂无价格")
        components.append(Comp.Plain("\n".join(lines)))
        break
    return components


async def _build_post_content(link: Dict[str, Any], cookie: str) -> List[Any]:
    """帖子正文：图文混排 / JSON 实体数组 / 纯 description 三种情况"""
    components: List[Any] = []
    raw_text = link.get("text")
    if not (isinstance(raw_text, str) and raw_text.strip().startswith(("[", "{"))):
        return components

    try:
        entities = json.loads(raw_text)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件][小黑盒] 正文 JSON 解析失败: {exc}")
        return components

    if isinstance(entities, dict):
        entities = [entities]

    html_item = next(
        (item for item in entities if isinstance(item, dict) and item.get("type") == "html" and item.get("text")),
        None,
    )

    if html_item:
        html_string = html_item["text"]
        parts = re.split(r"(<img .*?/?>|<iframe.*?</iframe>)", html_string, flags=re.S)
        buffer = ""

        for index, part in enumerate(parts):
            if not part:
                continue
            if part.startswith("<img"):
                cleaned = clean_html_text(buffer)
                if cleaned:
                    components.append(Comp.Plain(cleaned))
                buffer = ""

                game_match = re.search(r'data-gameid="(\d+)"', part)
                img_match = re.search(r'data-original="([^"]+)"', part)
                if game_match:
                    card = await _render_game_card(game_match.group(1), cookie)
                    if card:
                        components.extend(card)
                    else:
                        logger.warning(f"[R插件][小黑盒] 游戏卡片 {game_match.group(1)} 没取到数据")
                elif img_match:
                    img = await local_image(
                        optimize_image_url(img_match.group(1)), referer=XHH_REFERER, prefix="xhh_inline"
                    )
                    if img is not None:
                        components.append(img)
                else:
                    buffer += part
            elif part.startswith("<iframe"):
                cleaned = clean_html_text(buffer)
                if cleaned:
                    components.append(Comp.Plain(cleaned))
                buffer = ""
                src_match = re.search(r'src="([^"]+)"', part)
                if src_match:
                    src = src_match.group(1).replace("\\", "")
                    if src.startswith("//"):
                        src = "https:" + src
                    components.append(Comp.Plain(f"\n({src})\n"))
            else:
                buffer += part

        cleaned = clean_html_text(buffer)
        if cleaned:
            components.append(Comp.Plain(cleaned))
        return components

    # JSON 实体数组：图片和文本分开
    image_urls = [optimize_image_url(item["url"]) for item in entities
                  if isinstance(item, dict) and item.get("type") == "img" and item.get("url")]
    text_content = "\n".join(item["text"] for item in entities
                             if isinstance(item, dict) and item.get("type") == "text" and item.get("text"))
    for url in image_urls:
        components.append(Comp.Image.fromURL(url))
    if text_content and text_content != link.get("description"):
        components.append(Comp.Plain(text_content))
    return components


def _format_comment_time(value: Any) -> str:
    import datetime

    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return str(value or "")
    try:
        return datetime.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return ""


async def _handle_bbs(event: AstrMessageEvent, link_id: str, cookie: str, identify_prefix: str,
                      image_batch_threshold: int, video_size_limit_mb: int,
                      msg_element_limit: int = XHH_MSG_ELEMENT_LIMIT,
                      comment_limit: int = XHH_COMMENT_LIMIT) -> AsyncGenerator[Any, None]:
    payload = await _api_get(XHH_BBS_LINK, get_api_params("bbs", link_id), cookie)
    if not isinstance(payload, dict) or payload.get("status") != "ok" or not payload.get("result"):
        logger.error(f"[R插件][小黑盒] 帖子接口异常: {str(payload)[:200]}")
        yield event.plain_result("小黑盒帖子解析失败，请检查链接是否正确或 Cookie 是否过期")
        return

    result = payload["result"]
    link = result.get("link") or {}
    comments = result.get("comments") or []
    user = link.get("user") or {}

    header_components: List[Any] = []
    cover = link.get("thumb") or link.get("video_thumb")
    if cover:
        cover_comp = await local_image(optimize_image_url(cover), referer=XHH_REFERER, prefix="xhh_thumb")
        if cover_comp is not None:
            header_components.append(cover_comp)

    lines = [f"{identify_prefix}小黑盒帖子", f"👤作者：{user.get('username', '')}"]
    if link.get("title"):
        lines.append(f"📝标题：{link['title']}")
    if link.get("description"):
        lines.append(f"📄简介：{truncate_str(link['description'], 300)}")

    tags = ""
    if link.get("hashtags"):
        tags = " ".join(f"#{tag.get('name', '')}" for tag in link["hashtags"][:10] if tag.get("name"))
    elif link.get("content_tags"):
        tags = " ".join(f"#{tag.get('text', '')}" for tag in link["content_tags"][:10] if tag.get("text"))
    if tags:
        lines.append(f"🏷️标签：{tags}")

    header_components.append(Comp.Plain("\n" + "\n".join(lines)))
    yield event.chain_result(header_components)

    body = await _build_post_content(link, cookie)
    if body:
        for group in chunked(body, msg_element_limit):
            node = create_forward_message(
                [{"uin": event.get_sender_id(), "name": event.get_sender_name(), "content": group}],
                event.get_sender_name(),
                event.get_sender_id(),
            )
            try:
                yield event.chain_result([node])
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[R插件][小黑盒] 合并转发失败，改为直接发送: {exc}")
                yield event.chain_result(group)

    if link.get("has_video") == 1 and link.get("video_url"):
        path = await download_file(
            link["video_url"],
            build_temp_path("xiaoheihe", ".mp4"),
            referer="https://www.xiaoheihe.cn/",
            timeout=300,
        )
        if path:
            components = video_components(path, "xiaoheihe.mp4", video_size_limit_mb)
            if components:
                yield event.chain_result(components)
            else:
                yield event.plain_result(
                    f"小黑盒视频超过 {video_size_limit_mb}MB 发送上限\n原链接：{link.get('video_url')}"
                )
            release_video(path)
        else:
            yield event.plain_result("小黑盒视频下载失败")

    if comments:
        blocks: List[Dict[str, Any]] = []
        try:
            comments.sort(key=lambda thread: (thread.get("comment") or [{}])[0].get("floor_num", 0))
        except Exception:  # noqa: BLE001
            pass
        processed = 0
        for thread in comments:
            if processed >= comment_limit:
                break
            for comment in thread.get("comment") or []:
                if processed >= comment_limit:
                    break
                comment_user = (comment.get("user") or {}).get("username") or "小黑盒用户"
                text = clean_html_text(comment.get("text", ""))
                formatted_time = _format_comment_time(comment.get("create_at"))
                reply_user = (comment.get("replyuser") or {}).get("username")
                if reply_user:
                    head = f"{comment_user} 回复 {reply_user}\n{formatted_time}·{comment.get('ip_location', '')}"
                else:
                    head = f"{comment_user}\n{comment.get('floor_num', '')}楼 {formatted_time}·{comment.get('ip_location', '')}"
                content: List[Any] = [Comp.Plain(f"{head}\n\n{text}")]
                for img in comment.get("imgs") or []:
                    if img.get("url"):
                        img_comp = await local_image(
                            optimize_image_url(img["url"]), referer=XHH_REFERER, prefix="xhh_cmt"
                        )
                        if img_comp is not None:
                            content.append(img_comp)
                blocks.append({"uin": event.get_sender_id(), "name": comment_user, "content": content})
                processed += 1

        for group in chunked(blocks, msg_element_limit):
            node = create_forward_message(group, event.get_sender_name(), event.get_sender_id())
            try:
                yield event.chain_result([node])
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[R插件][小黑盒] 评论转发失败: {exc}")
                for block in group:
                    yield event.chain_result(block["content"])


async def _handle_game(event: AstrMessageEvent, kind: str, app_id: str, cookie: str, identify_prefix: str,
                       image_batch_threshold: int, video_size_limit_mb: int,
                       msg_element_limit: int = XHH_MSG_ELEMENT_LIMIT) -> AsyncGenerator[Any, None]:
    payload = await _api_get(GAME_API_MAP[kind], get_api_params(kind, app_id), cookie)
    if not isinstance(payload, dict) or not payload.get("result"):
        logger.error(f"[R插件][小黑盒] 游戏接口异常: {str(payload)[:200]}")
        yield event.plain_result("小黑盒游戏解析失败，请检查链接是否正确或 Cookie 是否过期")
        return

    data = payload["result"]
    head: List[Any] = [Comp.Plain(f"{identify_prefix}小黑盒游戏")]
    if data.get("image"):
        game_img = await local_image(optimize_image_url(data["image"]), referer=XHH_REFERER, prefix="xhh_game")
        if game_img is not None:
            head.append(game_img)

    brief: List[str] = []
    name = data.get("name") or ""
    name_en = f" ({data['name_en']})" if data.get("name_en") else ""
    if name or name_en:
        brief.append(f"🕹️ {name}{name_en}")
    if data.get("score"):
        brief.append(f"🌟 小黑盒评分: {data['score']} ({(data.get('comment_stats') or {}).get('score_comment', 0)}人评价)")
    if data.get("game_type") == "pc" and (data.get("price") or {}).get("current"):
        brief.append(f"💰 当前价格: ¥{data['price']['current']}")
    elif data.get("game_type") == "console" and data.get("region_prices"):
        region = data["region_prices"][0]
        brief.append(f"💰 当前价格: {region.get('final_amount')} ({region.get('region_name')})")
    if brief:
        head.append(Comp.Plain("\n" + "\n".join(brief)))
    yield event.chain_result(head)

    menu = data.get("menu_v2") or []

    def _menu_value(kind_name: str) -> str:
        for item in menu:
            if item.get("type") == kind_name and item.get("value"):
                return str(item["value"])
        return ""

    detail_parts: List[str] = []

    game_lines: List[str] = []
    if data.get("name"):
        game_lines.append(f"• 中文名: {data['name']}")
    if data.get("name_en"):
        game_lines.append(f"• 英文名: {data['name_en']}")
    if _menu_value("release_date"):
        game_lines.append(f"• 发行日期: {_menu_value('release_date')}")
    if _menu_value("developer"):
        game_lines.append(f"• 开发商: {_menu_value('developer')}")
    publishers = _menu_value("publisher")
    if not publishers and data.get("publishers"):
        publishers = ", ".join(p.get("value", "") for p in data["publishers"] if p.get("value"))
    if publishers:
        game_lines.append(f"• 发行商: {publishers}")
    if data.get("platforms"):
        game_lines.append(f"• 支持平台: {' / '.join(data['platforms'])}")
    if _menu_value("qq"):
        game_lines.append(f"• QQ交流群: {_menu_value('qq')}")
    if game_lines:
        detail_parts.append("--- ✨ 游戏信息 ✨ ---\n" + "\n".join(game_lines))

    if data.get("game_award"):
        award_lines = [
            f"• {award.get('detail_name', '')}" + (f" ({award['desc']})" if award.get("desc") else "")
            for award in data["game_award"]
        ]
        if award_lines:
            detail_parts.append("\n--- 🏆 游戏奖项 🏆 ---\n" + "\n".join(award_lines))

    price_lines: List[str] = []
    price = data.get("price") or {}
    if data.get("game_type") == "pc" and price:
        if price.get("current") not in (None, price.get("initial")):
            price_lines.append(f"• 当前价格: ¥{price['current']} (原价: ¥{price.get('initial')}, -{price.get('discount')}%)")
        elif price.get("current"):
            price_lines.append(f"• 当前价格: ¥{price['current']}")
        if price.get("lowest_price"):
            price_lines.append(f"• 史低价格: ¥{price['lowest_price']} (-{price.get('lowest_discount')}%)")
        heybox_price = data.get("heybox_price") or {}
        if heybox_price.get("cost_coin"):
            price_lines.append(f"• 小黑盒商城: {heybox_price['cost_coin'] / 1000} 盒币")
        if price.get("deadline_date"):
            price_lines.append(f"• 优惠截止: {price['deadline_date']}")
    if data.get("game_type") == "console" and data.get("region_prices"):
        region_lines = []
        for region in data["region_prices"]:
            line = f"{region.get('region_name')}: "
            if region.get("current") != region.get("initial"):
                line += f"{region.get('final_amount')} (原价: {region.get('initial_amount')}, -{region.get('discount')}%)"
            else:
                line += f"{region.get('final_amount')}"
            if region.get("lowest_price") and region["lowest_price"] != region.get("final_amount"):
                line += f" / 史低: {region['lowest_price']}"
            region_lines.append(line)
        price_lines.append("🌐 地区价格:\n  " + "\n  ".join(region_lines))
    if price_lines:
        detail_parts.append("\n--- 💰 价格信息 💰 ---\n" + "\n".join(price_lines))

    community: List[str] = []
    if data.get("score"):
        community.append(f"• 小黑盒评分: {data['score']} ({(data.get('comment_stats') or {}).get('score_comment', 0)}人评价)")
    if (data.get("comment_stats") or {}).get("star_5"):
        try:
            rate = float(data["comment_stats"]["star_5"]) / 100
            community.append(f"• 玩家好评率: {rate:.0%}")
        except (TypeError, ValueError):
            pass
    radar = data.get("multidimensional_score_radar") or {}
    if radar.get("dimension_list"):
        community.append("• 多维度评分:")
        for dim in radar["dimension_list"]:
            community.append(f"  - {dim.get('dimension_name')}: {dim.get('score')}")
    tag_source = data.get("common_tags") or data.get("hot_tags") or []
    tags = " ".join(f"#{tag['desc']}" for tag in tag_source if isinstance(tag, dict) and tag.get("desc"))
    if tags:
        community.append(f"• 热门标签: {tags}")
    for source_key in ("user_num", "game_data"):
        for item in (data.get(source_key) or {}).get("game_data") or []:
            desc = item.get("desc")
            value = item.get("value")
            if desc and value:
                community.append(f"• {desc}: {value}")
    if community:
        detail_parts.append("\n--- 🌟 社区评价 🌟 ---\n" + "\n".join(community))

    for item in data.get("game_data") or []:
        if item.get("desc") and item.get("value"):
            detail_parts.append(f"\n• {item['desc']}: {item['value']}")

    if data.get("game_type") == "pc":
        steam_tag = next((tag for tag in (data.get("common_tags") or []) if tag.get("type") == "steam_aggre"), None)
        if steam_tag:
            for item in steam_tag.get("detail_list") or []:
                if item.get("name") == "支持Steam Deck" and item.get("desc"):
                    detail_parts.append(f"\n--- 🎮 兼容性信息 🎮 ---\n• Steam Deck: {item['desc']}\n")

    if _menu_value("dlc"):
        detail_parts.append(f"\n--- 🧩 DLCs信息 🧩 ---\n• {_menu_value('dlc')}")

    if data.get("about_the_game"):
        detail_parts.append(f"\n--- 📖 游戏简介 📖 ---\n{truncate_str(data['about_the_game'], 1500)}")

    blocks: List[Dict[str, Any]] = []
    if detail_parts:
        blocks.append({
            "uin": event.get_sender_id(),
            "name": event.get_sender_name(),
            "content": [Comp.Plain("\n".join(detail_parts))],
        })

    screenshots = [optimize_image_url(item.get("url") or item.get("thumbnail"))
                   for item in data.get("screenshots") or []
                   if item.get("type") == "image" and (item.get("url") or item.get("thumbnail"))]
    if screenshots:
        shot_comps: List[Any] = [Comp.Plain("🖼️ 游戏截图 🖼️")]
        for url in screenshots:
            shot = await local_image(url, referer=XHH_REFERER, prefix="xhh_shot")
            if shot is not None:
                shot_comps.append(shot)
        if len(shot_comps) > 1:
            blocks.append({
                "uin": event.get_sender_id(),
                "name": event.get_sender_name(),
                "content": shot_comps,
            })

    for group in chunked(blocks, msg_element_limit):
        node = create_forward_message(group, event.get_sender_name(), event.get_sender_id())
        try:
            yield event.chain_result([node])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件][小黑盒] 游戏详情转发失败: {exc}")
            for block in group:
                yield event.chain_result(block["content"])

    movie = next((item for item in data.get("screenshots") or [] if item.get("type") == "movie" and item.get("url")), None)
    if movie:
        path = await download_file(
            movie["url"],
            build_temp_path("xiaoheihe_game", ".mp4"),
            referer="https://www.xiaoheihe.cn/",
            timeout=300,
        )
        if path:
            components = video_components(path, "xiaoheihe_game.mp4", video_size_limit_mb)
            if components:
                yield event.chain_result(components)
            else:
                yield event.plain_result(f"小黑盒游戏视频超过 {video_size_limit_mb}MB 发送上限")
            release_video(path)


async def process_xiaoheihe_url(
    event: AstrMessageEvent,
    cookie: str = "",
    identify_prefix: str = "识别：",
    image_batch_threshold: int = 50,
    video_size_limit_mb: int = 0,
    msg_element_limit: int = XHH_MSG_ELEMENT_LIMIT,
    comment_limit: int = XHH_COMMENT_LIMIT,
) -> AsyncGenerator[Any, None]:
    msg = get_message_text(event)
    if not msg:
        return

    kind, ident = _parse_target(msg)
    if not kind or not ident:
        logger.info(f"[R插件][小黑盒] 未解析到有效 ID: {msg[:120]}")
        return

    if not cookie:
        yield event.plain_result("检测到没有填写小黑盒 Cookie，无法解析小黑盒（配置项 XHH_CK）")
        return

    if kind == "bbs":
        generator = _handle_bbs(
            event, ident, cookie, identify_prefix, image_batch_threshold, video_size_limit_mb,
            msg_element_limit, comment_limit,
        )
    else:
        generator = _handle_game(
            event, kind, ident, cookie, identify_prefix, image_batch_threshold, video_size_limit_mb,
            msg_element_limit,
        )

    async for result in generator:
        yield result
"""OpenAI 兼容的流式 BrainPort（DeepSeek / Hermes / 本地 vLLM 都可）。

只依赖 stdlib；不把 API key 放进任何帧或快照。失败时先出 error 帧，再出空 done，
让上层照常收尾，而不是把整条线卡在 thinking。
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Iterator, Mapping

from agent.brain.messages import build_messages
from agent.brain.open_retry import open_with_retry
from agent.brain.turn_trace import log_turn
from agent.brain.markup_guard import MarkupGuard
from agent.brain.stream_guard import (accumulate_tool_calls, finish_calls, first_token_budget,
                                               first_token_late, relax_read_timeout,
                                               stream_failure)
from agent.brain.domain.entity import EntityRegistry
from agent.brain.domain.principal import PrincipalRegistry
from agent.brain.ports import BrainRequest


@dataclass(frozen=True, slots=True)
class OpenAICompatibleConfig:
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    api_key: str = ""
    timeout_s: float = 60.0
    temperature: float = 0.8
    max_tokens: int = 512
    #: 整段 prompt 的字符預算（owner 2026-10-22：放寬到 36K）。
    max_chars: int = 36000
    #: 最多幾條歷史（安全上限；實際由字符預算決定，見 `build_messages`）。
    max_history: int = 60
    #: 思考型模型（Qwen3 这类）默认先吐 `reasoning_content`；聊天场景关掉能快 ~4 倍。
    #: 2026-10-21 实测（本地 llama-server + qwen38-27b，同一句）：开 4.0 s / 关 1.0 s。
    enable_thinking: bool = True
    #: 推理强度（DeepSeek / OpenAI 风格）。现役桥用 `DEEPSEEK_THINKING=low`：闲聊 1 s 级。
    reasoning_effort: str = ""

    @property
    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "OpenAICompatibleConfig":
        values = os.environ if env is None else env
        # 主腦專用的思考檔位（owner 2026-10-24：「主脑可以保留 low 思考」）：
        # 全域 `BLUEAGENT_LLM_ENABLE_THINKING=0` 把子代理／狀態引出／重排都關掉，
        # 但主腦留一點思考（讀語感、分辨問句與指令）。設了這個就等於「開思考 + 指定檔位」。
        brain_effort = str(values.get("BLUEAGENT_BRAIN_REASONING_EFFORT") or "").strip().lower()
        if brain_effort in {"none", "low", "medium", "high", "max"}:
            enable_thinking = brain_effort != "none"
            reasoning_effort = "" if brain_effort == "none" else brain_effort
        else:
            enable_thinking = str(values.get("BLUEAGENT_LLM_ENABLE_THINKING") or "1").strip() \
                not in {"0", "off", "false", "no"}
            # 沿用现役桥的变量名：DEEPSEEK_THINKING=low ⇒ reasoning_effort=low
            reasoning_effort = str(values.get("BLUEAGENT_LLM_REASONING_EFFORT")
                                   or values.get("DEEPSEEK_THINKING") or "").strip().lower()
        return cls(
            base_url=str(values.get("BLUEAGENT_LLM_BASE_URL")
                         or "https://api.deepseek.com/v1").strip(),
            model=str(values.get("BLUEAGENT_LLM_MODEL")
                      or "deepseek-chat").strip(),
            api_key=str(values.get("BLUEAGENT_LLM_API_KEY")
                        or values.get("DEEPSEEK_API_KEY") or "").strip(),
            timeout_s=max(1.0, _float(values.get("BLUEAGENT_LLM_TIMEOUT_S"), 60)),
            temperature=_float(values.get("BLUEAGENT_LLM_TEMPERATURE"), 0.8),
            max_tokens=max(1, _int(values.get("BLUEAGENT_LLM_MAX_TOKENS"), 512)),
            max_chars=max(2000, _int(values.get("BLUEAGENT_LLM_MAX_CHARS"), 36000)),
            max_history=max(0, _int(values.get("BLUEAGENT_LLM_MAX_HISTORY"), 60)),
            enable_thinking=enable_thinking,
            reasoning_effort=reasoning_effort,
        )


def merge_leading_system(messages: list[dict]) -> list[dict]:
    """把开头连续的 `system` 合并成一条。

    为什么必须做：本地 llama-server（Qwen 模板）对两条 `system` 直接 500
    `System message must be at the beginning`（2026-10-21 实测：两条报错、一条正常）。
    人格一段 + 记忆一段是我们的正常形状 ⇒ 在这一层合并，语义不变，领域层不动。
    """
    parts: list[str] = []
    rest = list(messages)
    while rest and str(rest[0].get("role") or "") == "system":
        parts.append(str(rest.pop(0).get("content") or ""))
    merged = [part for part in parts if part.strip()]
    if merged:
        rest.insert(0, {"role": "system", "content": "\n\n".join(merged)})
    return rest


#: 語言不符時，重寫那一輪要說明白（不然模型只會換句話說）。
REWRITE_HINT: dict[str, str] = {
    "ja": "\n（前の返答は中国語だった。日本語だけで書き直して）",
    "zh": "\n（上一句写成日语了，只用中文重写）",
}


def _safe_tool_name(name: str) -> str:
    """OpenAI 兼容端点只认 `[a-zA-Z0-9_-]`（DeepSeek 实测：`voice.speak` 直接 400）。"""
    return "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in str(name or ""))


def _tool_payloads(tools) -> tuple[list[dict], dict[str, str]]:
    """送出去的 tool 名消毒；回 `(工具表, 消毒名→原名)`，收 tool_call 时反查。"""
    payloads: list[dict] = []
    back: dict[str, str] = {}
    for tool in tools or ():
        if not isinstance(tool, dict):
            continue
        item = dict(tool)
        function = dict(item.get("function") or {})
        original = str(function.get("name") or "")
        if original:
            safe = _safe_tool_name(original)
            function["name"] = safe
            back[safe] = original
            item["function"] = function
            payloads.append(item)
    return payloads, back


def _lang_verdict(text: str, lang: str) -> bool | None:
    """粗判這段文字是不是想要的語言：True＝是，False＝不是，None＝還看不出來。

    刻意只做「假名/漢字」這種機械判據——不呼叫模型、不看語意。
    日文：出現假名就放行；沒有假名卻有一堆漢字 ⇒ 判定為中文。
    中文：出現假名 ⇒ 判定為日文。
    """
    lang = str(lang or "").strip().lower()
    if lang not in {"ja", "zh"}:
        return True
    kana = sum(1 for ch in text if "\u3040" <= ch <= "\u30ff")
    hanzi = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    if lang == "ja":
        if kana:
            return True
        if hanzi >= 6:
            return False
        return None
    if kana:
        return False
    if hanzi:
        return True
    return None


def _rewrite_hint(messages: list[dict], lang: str) -> list[dict]:
    """把「上一句語言錯了」掛在最後一句用戶訊息尾端（模型對最後一句服從度最高）。"""
    hint = REWRITE_HINT.get(str(lang or "").strip().lower())
    if not hint:
        return messages
    out = [dict(row) for row in messages]
    for row in reversed(out):
        if str(row.get("role")) == "user":
            row["content"] = str(row.get("content") or "") + hint
            break
    return out


@dataclass
class OpenAICompatibleBrain:
    config: OpenAICompatibleConfig
    entities: EntityRegistry
    principals: PrincipalRegistry
    _last_usage: dict = field(default_factory=dict, init=False)
    #: 消毒後的 tool 名 → 原名（DeepSeek 不收帶點的名字）
    _tool_names: dict = field(default_factory=dict, init=False)

    #: 語言閘：先緩衝幾個字再判語言，不符就整輪重寫（別讓中文先流到端末）。
    LANG_GUARD_CHARS = 8
    #: 最多重寫幾次（第一次照常，第二次帶「重寫成日語」的提示）。
    LANG_ATTEMPTS = 2

    def run_turn(self, request: BrainRequest) -> Iterator[tuple[str, Mapping]]:
        entity = self.entities.get(request.entity)
        entity_name = entity.name if entity is not None else request.entity
        yield ("thinking", {"text": f"{entity_name}在想怎么接这句话…", "entity": request.entity})
        speak_lang = str(request.meta.get("speak_lang") or "").strip().lower()
        started = time.monotonic()
        stats: dict = {}
        first_ms = 0
        attempt = 0
        while True:
            attempt += 1
            messages = merge_leading_system(
                build_messages(request, max_history=self.config.max_history,
                               max_chars=self.config.max_chars, stats=stats))
            if attempt > 1:
                messages = _rewrite_hint(messages, speak_lang)
                yield ("lang_retry", {"speak_lang": speak_lang,
                                      "attempt": attempt - 1})
            outcome: dict = {"text": [], "calls": [], "usage": {},
                             "failed": False, "lang_bad": False}
            body = self._body(request, messages)
            stream = self._stream_turn(request, body, speak_lang, outcome,
                                       last_attempt=attempt >= self.LANG_ATTEMPTS)
            for event in stream:
                first_ms = first_ms or (int((time.monotonic() - started) * 1000)
                                        if event[0] == "delta" else 0)
                yield event
            log_turn(request.entity, first_ms=first_ms,
                     total_ms=int((time.monotonic() - started) * 1000),
                     outcome=outcome, stats=stats, attempts=attempt, body=body)
            if outcome["failed"]:
                return
            # 模型一个字都没给（思考把 token 吃光／偶发空回；DeepSeek 实测三路并发时会出现）
            # ⇒ 再要一次，别让用户看到空回复。
            if not outcome["text"] and not outcome["calls"] and attempt < self.LANG_ATTEMPTS:
                yield ("retry", {"reason": "empty_response", "attempt": attempt})
                continue
            if outcome["lang_bad"] and attempt < self.LANG_ATTEMPTS:
                continue
            break

        text, calls, usage = "".join(outcome["text"]).strip(), outcome["calls"], outcome["usage"]
        if calls:
            for index, call in enumerate(calls):
                yield ("tool_call", {
                    "id": str(call.get("id") or f"call_{index + 1}"),
                    "name": str(call["name"]),
                    "arguments": _parse_arguments(call.get("arguments")),
                })
            # 有些模型會在 tool call 前先說一句；有正文就照常收尾，
            # 沒有正文則讓 BrainStage 執行工具後再來一輪。
            if not text:
                return
        if not text:
            yield from self._failed(request, "empty_response", "模型没有返回内容")
            return
        self._last_usage = usage
        payload = {"text": text, "recall": list(request.meta.get("recall", ())),
                   "entity": request.entity, "principal": request.principal,
                   "model": self.config.model}
        if usage:
            payload["usage"] = usage
        yield ("done", payload)

    def _body(self, request: BrainRequest, messages: list[dict]) -> dict:
        body = {
            "model": self.config.model,
            "messages": messages,
            "stream": True,
            "temperature": self.config.temperature,
            # 單輪覆寫：`request.meta["max_tokens"]` 優先（TRPG 那一輪需要比 400 更大的額度，
            # 不然 reasoning 就吃光、content 回空）
            "max_tokens": int((request.meta or {}).get("max_tokens") or self.config.max_tokens),
        }
        # 關思考的正確寫法（DeepSeek 認 thinking / reasoning_effort；本機 vLLM 認
        # chat_template_kwargs）——2026-10-24 實測：以前那個鍵 DeepSeek 直接忽略，
        # 所以「關掉思考」其實一直沒生效。詳見 `config/llm.thinking_payload`。
        from agent.brain.config import llm as _llm
        body.update(_llm.thinking_payload(enable=bool(self.config.enable_thinking),
                                          effort=str(self.config.reasoning_effort or ""),
                                          base_url=str(self.config.base_url or "")))
        # 2026-11-02 本地化：**钉住同一个 KV 槽位**，否则前缀快取白费。
        # 实测（Strata，同一份 5.2K prompt、4 槽轮转）：
        #   · 不指定 → 相邻两轮落到不同槽，cached_tokens = 0，prefill 每次 3 秒
        #   · id_slot=0 → 同一前缀的下一次请求 cached 5,180/5,187，prefill 掉到 0.33 秒
        # 远端（DeepSeek）不认这个字段，所以只在本地端点加。
        if _llm.is_local(str(self.config.base_url or "")):
            # 通道分配（owner 2026-11-02）：**三個實體各一條固定通道**，主腦（chat）用它自己的；
            # 狀態引出／子代理／蒸餾／看圖這些「短 prompt、不靠快取」的呼叫共用第 4 條 scratch，
            # 這樣它們不會把該實體剛累積的對話前綴撞掉。
            # 可用 env 覆寫：BLUEAGENT_ENTITY_SLOTS="mira:0,aster:1,nori:2"、BLUEAGENT_LLM_SCRATCH_SLOT=3
            role = (str(getattr(self.config, "role", "") or "") or "chat").upper()
            raw = (request.meta or {}).get("id_slot")
            if raw is None:
                if role == "CHAT":
                    raw = _entity_slot(str(getattr(request, "entity", "") or ""))
                else:
                    raw = (os.environ.get(f"BLUEAGENT_LLM_{role}_SLOT")
                           or os.environ.get("BLUEAGENT_LLM_SCRATCH_SLOT") or 3)
            try:
                body["id_slot"] = int(raw)
            except (TypeError, ValueError):
                body["id_slot"] = 0
            # 2026-11-02 熱機哨兵的正式替代方案（owner「給人格做一個 KV 錨點」）：
            # Strata 原生有這個機制 = `strata_prefix`（引擎側 `pin=N`）。宣告「前 K 條訊息是共享唯讀前綴」，
            # 引擎就把它們 tokenize、在邊界把 checkpoint **釘住（永不淘汰）**，且**不把後續分支 re-park**，
            # 所以 N 輪只付一次前綴的錢。前綴內容一改（換 persona 檔）邊界自己重算，不需要外部哨兵。
            # ⚠️ 只釘 1 條：`run_turn` 先過 `merge_leading_system`，stance＋人格穩定半＋技能＋貼圖
            #    已經合成**一條** system。寫 2 會把第二條也釘進去——那是最早一條歷史（窗口一滾就變），
            #    沒歷史時更是本輪用戶訊息（心情／私記／記憶每輪都變）⇒ 每輪多釘一個永不淘汰的 checkpoint。
            if role == "CHAT" and str(os.environ.get("BLUEAGENT_STRATA_PREFIX") or "0") \
                    .strip().lower() not in {"0", "off", "false", "no"}:
                body["strata_prefix"] = {"messages": 1}     # [合併後的固定段 system]
            # ⛔ **2026-11-02 23:1x 實測：這個欄位現在會讓整輪直接 400**，所以預設關掉。
            #    引擎回 `No user query found in messages.`（curl 二分：system+user ＝ 200；
            #    同一個 body 再加 `strata_prefix:{messages:1}` ＝ 400）。症狀是「她答空」：
            #    請求 8 ms 就回來、`first_token=null`、outbox `text=""`——2026-11-02 22:08 與
            #    23:05 兩次「收不到回覆」都是它。要重開先確認引擎那一側的語意（prefix 是
            #    「前 N 條」還是「除最後 N 條之外」），並用上面那組 curl 二分驗過再開。
        tools = request.meta.get("tools")
        if isinstance(tools, list) and tools:
            payloads, self._tool_names = _tool_payloads(tools)
            if payloads:
                body["tools"] = payloads
                body["tool_choice"] = "auto"
        return body

    def _stream_turn(self, request: BrainRequest, body: dict, speak_lang: str,
                     outcome: dict, *, last_attempt: bool = False) -> Iterator[tuple[str, Mapping]]:
        """跑一次流式请求。正文先扣在 `buffer` 裡，語言過閘才放出去。

        `last_attempt=True` ⇒ 最後一次機會：語言還是不對也照樣放出去
        （寧可語言不對，也不能整輪沒字）。
        """
        headers = {"Content-Type": "application/json",
                   "Accept": "text/event-stream",
                   "User-Agent": "BlueAgentNext/0.1"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        # 超時分兩層（owner 2026-11-02）：整次請求用 `timeout_s`；`deadline_ms` 只管第一個字。
        timeout = float(self.config.timeout_s)
        first_token_s, started, first_seen = first_token_budget(request), time.monotonic(), False
        req = urllib.request.Request(
            self.config.endpoint, data=json.dumps(body).encode("utf-8"),
            headers=headers, method="POST")
        buffer: list[str] = []
        flushed = False
        pending_calls: dict[int, dict] = {}
        # 文字形式的工具呼叫（實機 bug 2026-11-03）：模型把 `<tool_call>…` 吐在正文裡，
        # 這道門負責壓住／解析／吃掉，別讓它流到端末當成她說的話。
        guard = MarkupGuard()
        try:
            # 連線與等第一個字用**首幀預算**；第一個字一到就放寬成整次請求的上限
            # （不然長回答會被首幀那條切斷）。
            open_timeout = min(timeout, max(1.0, first_token_s)) if first_token_s else timeout
            response = yield from self._open(request, req, open_timeout)
            with response:
                for event in self._events(response):
                    if first_token_late(first_seen, first_token_s, started):
                        outcome["failed"] = True
                        yield from self._failed(
                            request, "first_token_timeout",
                            f"第一個字 {int(first_token_s * 1000)} ms 內沒到")
                        return
                    choices = event.get("choices")
                    if isinstance(choices, list) and choices:
                        choice = choices[0] if isinstance(choices[0], dict) else {}
                        if choice.get("finish_reason"):
                            outcome["finish_reason"] = choice.get("finish_reason")
                        delta = choice.get("delta")
                        if isinstance(delta, dict):
                            _reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                            if _reasoning:
                                outcome["reasoning_chars"] = outcome.get("reasoning_chars", 0) + len(str(_reasoning))
                            # 思考流與直接調工具都算「有輸出」（她在想，不是在掛；`stream_guard.first_token_late`）
                            # ——思考不算的話，開了思考的那一輪會被首幀預算判 `first_token_timeout`、答空
                            if (_reasoning or delta.get("tool_calls")) and not first_seen:
                                first_seen = True
                                relax_read_timeout(response, timeout)
                            piece = _text(delta.get("content"))
                            if piece:
                                piece = guard.feed(piece)      # 文字形式的工具呼叫：壓住／解析／吃掉
                            if piece:
                                if not first_seen:
                                    first_seen = True          # 第一個字到了 ⇒ 放寬讀取超時
                                    relax_read_timeout(response, timeout)
                                if flushed:
                                    outcome["text"].append(piece)
                                    yield ("delta", {"text": piece,
                                                     "entity": request.entity})
                                else:
                                    buffer.append(piece)
                                    verdict = _lang_verdict("".join(buffer), speak_lang)
                                    if verdict is False and not last_attempt:
                                        # 語言不符：整輪丟掉，回去重寫（不放出任何字）
                                        outcome["lang_bad"] = True
                                        print(f"  brain[{request.entity}] 中途語言閘擋下："
                                              f"speak_lang={speak_lang!r} buffer_head={''.join(buffer)[:60]!r}",
                                              flush=True)
                                        return
                                    if verdict is True:
                                        chunk = "".join(buffer)
                                        buffer = []
                                        flushed = True
                                        outcome["text"].append(chunk)
                                        yield ("delta", {"text": chunk,
                                                         "entity": request.entity})
                            accumulate_tool_calls(pending_calls, delta.get("tool_calls"))
                    if isinstance(event.get("usage"), dict):
                        outcome["usage"] = dict(event["usage"])
                # 串流收尾：guard 裡若還壓著東西，照同一套規矩處理（殘骸一律不吃進正文）
                tail = guard.flush()
                if tail:
                    if flushed:
                        outcome["text"].append(tail)
                        yield ("delta", {"text": tail, "entity": request.entity})
                    else:
                        buffer.append(tail)
                        if (last_attempt or not speak_lang
                                or _lang_verdict("".join(buffer), speak_lang) is not False):
                            chunk = "".join(buffer)
                            buffer = []
                            flushed = True
                            outcome["text"].append(chunk)
                            yield ("delta", {"text": chunk, "entity": request.entity})
        except urllib.error.HTTPError as exc:
            detail = _safe_body(exc)
            outcome["failed"] = True
            yield from self._failed(request, f"http_{exc.code}", detail)
            return
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            outcome["failed"] = True
            code, detail = stream_failure(request, first_token_s, first_seen, started, exc)
            yield from self._failed(request, code, detail)
            return
        except (ValueError, TypeError) as exc:
            outcome["failed"] = True
            yield from self._failed(request, "bad_response",
                                    f"{type(exc).__name__}: {str(exc)[:160]}")
            return

        # ── 卡點取證：她「回空」時要看得出是哪一種（空串流／語言閘／被截斷）──
        try:
            _text_len = sum(len(part) for part in outcome.get("text") or [])
            _usage = outcome.get("usage") or {}
            if _text_len == 0 or outcome.get("lang_bad") or outcome.get("failed"):
                print(f"  brain[{request.entity}] 回空取證：text={_text_len}字 "
                      f"lang_bad={outcome.get('lang_bad')} failed={outcome.get('failed')} "
                      f"buffer={len(buffer)}字 speak_lang={speak_lang!r} "
                      f"reasoning={outcome.get('reasoning_chars', 0)}字 "
                      f"finish={outcome.get('finish_reason')!r} "
                      f"usage={_usage} buffer_head={(''.join(buffer))[:60]!r}", flush=True)
        except Exception:                               # noqa: BLE001
            pass

        if buffer and not flushed:
            chunk = "".join(buffer)
            if (last_attempt or not speak_lang
                    or _lang_verdict(chunk, speak_lang) is not False):
                outcome["text"].append(chunk)
                yield ("delta", {"text": chunk, "entity": request.entity})
            else:
                outcome["lang_bad"] = True
                print(f"  brain[{request.entity}] 尾段語言閘擋下：speak_lang={speak_lang!r} "
                      f"chunk_head={chunk[:60]!r}", flush=True)
                return
        outcome["calls"] = finish_calls(pending_calls, guard.swallowed,
                                        getattr(self, "_tool_names", {}) or {},
                                        request.entity, outcome.get("finish_reason"))
    def _open(self, request: BrainRequest, req, timeout: float):
        """送出請求（未出字前遇 408／429／5xx 會等會兒再送）；細節見 `brain/open_retry.py`。"""
        return open_with_retry(request, req, timeout, sleep=self._sleep, safe_body=_safe_body)

    #: 測試換成假的（不真的睡）
    _sleep = staticmethod(time.sleep)

    def _failed(self, request: BrainRequest, code: str,
                detail: str) -> Iterator[tuple[str, Mapping]]:
        yield ("error", {"error": code, "text": detail, "entity": request.entity})
        yield ("done", {"text": "", "error": code, "entity": request.entity,
                        "principal": request.principal,
                        "model": self.config.model, "recall": []})

    @staticmethod
    def _events(response) -> Iterator[dict]:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event


def _parse_arguments(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
        return value if isinstance(value, dict) else {"_value": value}
    except (TypeError, ValueError):
        return {"_raw": str(raw)[:400]}


def _float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _entity_slot(entity: str) -> int:
    """實體 → 固定通道號（owner 2026-11-02「三個實體固定成 3 槽」）。

    `BLUEAGENT_ENTITY_SLOTS="mira:0,aster:1,nori:2"` 可覆寫；沒設定就用預設表，
    查不到回 0。同一實體永遠同一條通道，前綴快取才留得住。
    """
    table = {"mira": 0, "aster": 1, "nori": 2}
    raw = str(os.environ.get("BLUEAGENT_ENTITY_SLOTS") or "").strip()
    if raw:
        for item in raw.split(","):
            key, _, value = item.partition(":")
            key, value = key.strip(), value.strip()
            if not key or not value:
                continue
            try:
                table[key] = int(value)
            except ValueError:
                continue
    return int(table.get(str(entity or "").strip(), 0))


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(str(item.get("text") or "") if isinstance(item, dict)
                       else str(item) for item in value)
    return ""


def _safe_body(exc: urllib.error.HTTPError) -> str:
    # 重試時已經讀過一次（body 只能讀一次）⇒ 記在 exc 上，最後失敗那一下照樣拿得到
    cached = getattr(exc, "_ba_body", None)
    if cached is not None:
        return cached
    try:
        raw = exc.read()
        text = raw.decode("utf-8", "replace")[:400]
    except Exception:                                  # noqa: BLE001
        text = str(exc)[:160]
    try:
        exc._ba_body = text
    except Exception:                                  # noqa: BLE001
        pass
    return text


#: 這些狀態碼代表「伺服器現在忙／暫時壞」，還沒輸出時可以重送。
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
#: 沒有 `Retry-After` 時的等待（秒）；元素個數＝最多重送幾次。
RETRY_DELAYS = (3.0, 6.0, 12.0)
#: `Retry-After` 比這還長就不等了（她不能沉默半分鐘以上）。
RETRY_AFTER_MAX_S = 30.0


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    """讀 `Retry-After`（秒數或 HTTP 日期）；讀不懂回 None。"""
    headers = getattr(exc, "headers", None)
    raw = str(headers.get("Retry-After") or "").strip() if headers is not None else ""
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone
        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, IndexError):
        return None


__all__ = ["OpenAICompatibleBrain", "OpenAICompatibleConfig"]

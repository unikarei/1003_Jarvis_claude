"""答えを文ごとに読み上げる（声での会話、T-1213）。

逐次表示の差分を受け取り、「。」「？」「！」・改行で文に区切って、キューで順に読み上げる。
既定は macOS 標準の say（日本語の声 Kyoko）。URL・出典の番号・コード・表の記号は読まない。
"""

import queue
import re
import subprocess
import threading
from collections.abc import Callable

SENTENCE_END = re.compile(r"(?<=[。！？!?])|\n")
URL = re.compile(r"https?://\S+")
REFERENCE = re.compile(r"\[\d+\](?:\[\d+\])*")
MARKUP = re.compile(r"[#*`|>]+|^\s*[-・]\s*", re.M)


PAREN = re.compile(r"（[^（）]*）|\([^()]*\)")


def speakable(text: str) -> str:
    """読み上げる文字だけにする（URL・出典の番号・Markdown の記号・表の区切り線を除く）。"""
    if re.fullmatch(r"[\s|:\-]*", text):
        return ""
    text = URL.sub("", text)
    text = REFERENCE.sub("", text)
    text = MARKUP.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+([。、！？])", r"\1", text)


def _plain(text: str) -> str:
    """括弧の補足（プロジェクト・日付など）と下線を除いて、話し言葉にする。"""
    return speakable(PAREN.sub("", text).replace("_", " "))


CONNECTIONS_HEAD = "ナレッジの接続状況"     # knowledge_connections.report() の見出し


def _connections_summary(lines: list[str]) -> str:
    """接続の報告の読み上げ: 検索DB・Vault ごと・保存待ち（T-3101。前は見出しだけを読んでいた）。"""
    parts = []
    for line in lines[1:]:
        if line.startswith("検索DB"):
            m = re.search(r"知識 (\d+) 件", line)
            parts.append(f"検索DBは知識 {m.group(1)} 件" if m else "検索DBは未接続" if "未接続" in line else "検索DBは未確認")
            continue
        m = re.match(r"^([\w.-]+): (.*) / 検索DBの知識 (\S+) 件$", line)
        if m:
            state = "使えます" if m.group(2).startswith("存在") else _plain(m.group(2))
            count = f"{m.group(3)} 件" if m.group(3) != "未確認" else "件数は未確認"
            parts.append(f"{m.group(1).replace('-', ' ')}は{state}、{count}")
            continue
        m = re.match(r"^CKC保存待ち: (\S+) 件", line)
        if m:
            parts.append(f"保存待ちは {m.group(1)} 件" if m.group(1) != "未確認" else "保存待ちは未確認")
    return "。".join(parts) + "です。" if parts else CONNECTIONS_HEAD


def speech_summary(skill: str, text: str) -> str:
    """一覧・レポートを返すスキルの、読み上げ用の短い要約（声での会話、T-1214）。"""
    lines = [line for line in text.splitlines() if line.strip()]
    if skill == "knowledge-search":
        items = [line for line in lines if re.match(r"^\d+\. ", line)]
        if not items:
            return _plain(lines[0]) if lines else ""
        first = re.sub(r"^\d+\. ", "", items[0])
        return f"{len(items)} 件見つかりました。1 件目は、{_plain(first)}。"
    if skill == "knowledge-trace":
        source = next((line for line in lines if "← 出典:" in line), None)
        if source:
            return f"出典は、{_plain(source.split('← 出典:', 1)[1])}です。"
        return _plain(lines[0]) if lines else ""
    if skill == "knowledge-stats" and lines and lines[0].startswith(CONNECTIONS_HEAD):
        return _connections_summary(lines)
    if skill == "knowledge-stats":
        items = []
        for line in lines:
            m = re.match(r"^\d+\. (.*): (\d+) 件", line)
            if m and len(items) < 3:
                name = PAREN.sub("", m.group(1)).replace("「", " ").replace("」", "")
                items.append(f"{speakable(name) or '分野なし'}、{m.group(2)} 件")
        if not items:
            return _plain(lines[0]) if lines else ""
        return "上位は、" + "。".join(items) + "です。"
    if skill == "report-digest" and lines:
        if lines[0].startswith("Insight Reporter のレポート（"):
            kinds = sum(1 for line in lines if line.startswith("- "))
            return f"Insight Reporter のレポートは {kinds} 種類です。一覧を画面に出しました。"
        if re.search(r"の記事は \d+ 件です。$", lines[0]):
            return _plain(lines[0]) + "題目は画面に並べました。"
        return summary_line(text)
    if skill == "news-digest" and lines:
        return _news_summary(lines)
    if skill == "system-status" and lines:
        counts = re.match(r"(エラー \d+ 件・警告 \d+ 件)", lines[0])
        errors = []
        for line in lines:
            m = re.match(r"- `([^`]+)`（[^）]*）: (.*)", line)
            if m:
                message = speakable(m.group(2)).rstrip(":： ")
                errors.append(f"{m.group(1).replace('-', ' ')}、{message}")
                break
        head = counts.group(1).replace("・", "、") + "です。" if counts else _plain(lines[0])
        return head + (f"エラーは、{errors[0]}。" if errors and "エラー 0 件" not in lines[0] else "")
    return summary_line(text)


def _news_summary(lines: list[str]) -> str:
    """主要なニュース（T-3300）: 件数と、上位 3 件の見出しを順に読む。"""
    head = lines[0]
    m = re.search(r"(\d+月\d+日)(?:（.）)?(?:〜(\d+月\d+日)(?:（.）)?)?に取り込んだ(.*?)ニュース (\d+) 件", head)
    items = []
    for line in lines:
        item = re.match(r"^(\d+)\. \[\d+\] (.+?)（[^（）]*）— ", line)
        if item and len(items) < 3:
            items.append(f"{item.group(1)} 位、{speakable(item.group(2))}")
    if not m or not items:
        return summary_line("\n".join(lines))
    span = m.group(1) + (f"から{m.group(2)}" if m.group(2) else "")
    return f"{span}の{m.group(3)}ニュース {m.group(4)} 件のうち、" + "。".join(items) + "。"


def summary_line(text: str) -> str:
    """一覧やレポートは、最初の意味のある 1 行だけを読む。"""
    for line in text.splitlines():
        line = speakable(line)
        if line and not line.startswith("---"):
            return line
    return ""


class Speaker:
    """文を順に読み上げる。stop() で読み上げ中の文とキューを捨てる。"""

    def __init__(self, voice: str = "Kyoko", device: str | None = None,
                 runner: Callable[[list[str]], subprocess.Popen] | None = None):
        self.voice, self.device = voice, device
        self._runner = runner or (lambda cmd: subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                                               stderr=subprocess.DEVNULL))
        self._buffer = ""
        self._in_code = False
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._current: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _command(self, sentence: str) -> list[str]:
        cmd = ["say", "-v", self.voice]
        if self.device:
            cmd += ["-a", self.device]
        return cmd + [sentence]

    def _loop(self) -> None:
        while True:
            sentence = self._queue.get()
            if sentence is None:
                return
            with self._lock:
                self._current = self._runner(self._command(sentence))
            self._current.wait()
            with self._lock:
                self._current = None
            self._queue.task_done()

    def say(self, text: str) -> None:
        """1 文をそのまま読む（読めない記号は除く）。"""
        text = speakable(text)
        if text:
            self._queue.put(text)

    def feed(self, chunk: str) -> None:
        """逐次表示の差分を受け取り、文が完成したら読み上げのキューに入れる。コードの区間は読まない。"""
        self._buffer += chunk
        parts = SENTENCE_END.split(self._buffer)
        self._buffer = parts.pop()
        for part in parts:
            if part.strip().startswith("```"):
                self._in_code = not self._in_code
                continue
            if not self._in_code:
                self.say(part)

    def flush(self) -> None:
        """残りを読む。"""
        rest, self._buffer = self._buffer, ""
        if rest and not self._in_code:
            self.say(rest)
        self._in_code = False

    def stop(self) -> None:
        """読み上げを止め、キューを捨てる。"""
        self._buffer, self._in_code = "", False
        try:
            while True:
                self._queue.get_nowait()
                self._queue.task_done()
        except queue.Empty:
            pass
        with self._lock:
            if self._current is not None and self._current.poll() is None:
                self._current.terminate()

    def speaking(self) -> bool:
        with self._lock:
            busy = self._current is not None
        return busy or not self._queue.empty()

    def wait(self) -> None:
        self._queue.join()

    def close(self) -> None:
        self.stop()
        self._queue.put(None)

"""VOICEVOX ENGINE（ローカルHTTP API）でテキストを音声ファイル（wav）に合成する。

VOICEVOX ENGINEは別途ダウンロード・起動が必要（本リポジトリには含まない）。
https://voicevox.hiroshiba.jp/
"""

import requests

VOICEVOX_BASE_URL = "http://127.0.0.1:50021"
DEFAULT_SPEAKER_ID = 3  # ずんだもん（ノーマル）。ENGINE側の /speakers で確認・変更可能


def synthesize(text: str, output_path: str, speaker: int = DEFAULT_SPEAKER_ID) -> None:
    """テキストをVOICEVOX ENGINEで音声合成し、wavファイルとして保存する。"""
    try:
        query_res = requests.post(
            f"{VOICEVOX_BASE_URL}/audio_query",
            params={"text": text, "speaker": speaker},
            timeout=10,
        )
        query_res.raise_for_status()
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(
            f"VOICEVOX ENGINEに接続できません（{VOICEVOX_BASE_URL}）。"
            "ENGINEを起動してから実行してください。"
        ) from e

    synth_res = requests.post(
        f"{VOICEVOX_BASE_URL}/synthesis",
        params={"speaker": speaker},
        json=query_res.json(),
        timeout=30,
    )
    synth_res.raise_for_status()

    with open(output_path, "wb") as f:
        f.write(synth_res.content)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("使い方: python text_to_speech.py <テキスト> <出力wavパス>")
        sys.exit(1)

    synthesize(sys.argv[1], sys.argv[2])
    print(f"音声を出力しました: {sys.argv[2]}")

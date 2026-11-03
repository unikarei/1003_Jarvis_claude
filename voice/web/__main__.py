"""ブラウザでの声の会話を起動する（T-1501・T-1502）。

使い方: python -m voice.web [--port 8771] [--allow-host NAME] [--tailnet-port 8446]
表示された URL を手元の PC のブラウザで開く（VS Code のポート転送で別の番号になったら、その番号に読み替える）。
Mac の入力・出力の機器は調べない・開かない（sounddevice を import しない）。Ctrl-C で終了。
"""

import argparse
import sys

from voice.web.server import DEFAULT_PORT, make_server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ブラウザでの声の会話（127.0.0.1 だけ）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--allow-host", action="append", default=[], metavar="NAME")
    parser.add_argument("--tailnet-port", type=int, default=8446, metavar="N")
    args = parser.parse_args(argv)

    import orchestrator
    import speech_to_text
    from db import init_db

    init_db()
    print("音声認識のモデルを読み込んでいます…", flush=True)
    speech_to_text.load_model()
    import voice_chat
    voice_chat.use_voice_model()           # claude -p を速いモデルで起動する（待機させる前に）
    orchestrator.start_warm_up()
    server, token = make_server(args.port, allowed_hosts=args.allow_host)
    print(f"http://localhost:{server.server_address[1]}/?token={token}", flush=True)
    for name in server.allowed_hosts:
        print(f"https://{name}:{args.tailnet_port}/?token={token}", flush=True)
    print("（この URL は他人に渡さない。Ctrl-C で終了）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.sessions.end_current()      # この会話が起動した音声のエンジンを止める
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

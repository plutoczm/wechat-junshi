"""Local acceptance checks for the Windows WeChat bridge.

Default mode is read-only. --send-test requires an exact contact display name and sends
one explicit test text with database verification.
"""
import argparse
import json
import os
from pathlib import Path

from .wechat_bridge import WeChatBridge


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contact", help="search a private contact without sending")
    parser.add_argument("--send-test", help="exact private-contact display name for one verified test send")
    args = parser.parse_args()

    data = Path(os.environ.get("JUNSHI_DATA_DIR", str(Path.home() / ".wechat-junshi")))
    bridge = WeChatBridge(data, enabled=True)
    status = bridge.status(force=True)
    safe = {
        "bridge_enabled": status["enabled"],
        "package_installed": status["installed"],
        "package_version": status["package_version"],
        "wechat_connected": status["connected"],
        "can_read_history": status["can_read_history"],
        "can_send": status["can_send"],
        "target_client": status["verified_client"],
        "deepseek_key_configured": bool(os.environ.get("DEEPSEEK_API_KEY")),
    }
    print(json.dumps(safe, ensure_ascii=False, indent=2))
    if not status["connected"]:
        raise SystemExit(2)

    query = args.contact or args.send_test
    hits = bridge.search_contacts(query) if query else []
    if args.contact:
        print("matches:")
        for hit in hits:
            print("-", hit["display_name"])

    if args.send_test:
        exact = [h for h in hits if h["display_name"] == args.send_test]
        if len(exact) != 1:
            raise SystemExit("send-test requires exactly one matching private contact")
        hit = exact[0]
        result = bridge.send_text(
            hit["external_id"],
            hit["display_name"],
            "wechat-junshi 桥接验收：如果你看到这条消息，发送与本地回读链路已完成测试。",
        )
        print("send_test:", result["state"], result.get("detail", ""))
        if result["state"] != "confirmed":
            raise SystemExit(3)


if __name__ == "__main__":
    main()

"""Настроить выделенного Telegram-бота без передачи токена в аргументах или файлах."""

import argparse
import getpass
import json
import re
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default="mtc")
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--operators", required=True, help="Числовые ID разрешенных операторов через запятую")
    args = parser.parse_args()
    subprocess.run(["sh", "scripts/require-context.sh", args.cluster], check=True)
    if not re.fullmatch(r"-?[1-9][0-9]*", args.chat_id):
        raise SystemExit("Ожидается числовой ID выделенного тестового чата")
    operators = args.operators.split(",")
    if not operators or any(not re.fullmatch(r"[1-9][0-9]*", item.strip()) for item in operators):
        raise SystemExit("Ожидаются положительные ID операторов через запятую")
    token = getpass.getpass("Токен нового бота (ввод скрыт): ")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{25,}", token):
        raise SystemExit("Формат токена не распознан")
    body = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "telegram", "namespace": "heal"},
            "type": "Opaque", "stringData": {"token": token, "chat_id": args.chat_id,
                                            "allowed_user_ids": ",".join(item.strip() for item in operators)}}
    result = subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(body), text=True, capture_output=True)
    if result.returncode:
        raise SystemExit("Secret Telegram не сохранен; значения не выведены")
    print("Secret Telegram сохранен. Выполнить make observability и заменить под healer.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("Настройка отменена")

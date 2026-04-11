"""
races.json を ConoHa WING に FTP転送するスクリプト
環境変数:
  FTP_HOST     : FTPホスト名 (例: ftp.xxxxxxxx.conohawing.com)
  FTP_USER     : FTPユーザー名
  FTP_PASS     : FTPパスワード
  FTP_REMOTE   : アップロード先パス (例: /public_html/races.json)
"""

import ftplib
import os
import sys

def upload():
    host     = os.environ.get("FTP_HOST")
    user     = os.environ.get("FTP_USER")
    password = os.environ.get("FTP_PASS")
    remote   = os.environ.get("FTP_REMOTE", "/public_html/races.json")

    if not all([host, user, password]):
        print("ERROR: 環境変数 FTP_HOST / FTP_USER / FTP_PASS が未設定です")
        sys.exit(1)

    print(f"FTP接続: {host}")
    try:
        with ftplib.FTP(host, timeout=30) as ftp:
            ftp.login(user, password)
            ftp.set_pasv(True)
            print(f"ログイン成功: {ftp.getwelcome()}")

            # ディレクトリを再帰的に作成
            remote_dir = "/".join(remote.split("/")[:-1])
            dirs = remote_dir.split("/")
            path = ""
            for d in dirs:
                if not d:
                    continue
                path += "/" + d
                try:
                    ftp.mkd(path)
                    print(f"ディレクトリ作成: {path}")
                except ftplib.error_perm:
                    pass  # すでに存在する場合はスキップ

            with open("races.json", "rb") as f:
                ftp.storbinary(f"STOR {remote}", f)

            print(f"アップロード完了: {remote}")
    except ftplib.all_errors as e:
        print(f"FTPエラー: {e}")
        sys.exit(1)

if __name__ == "__main__":
    upload()

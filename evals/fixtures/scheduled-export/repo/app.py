import os
import urllib.request


def upload(path="summary.csv"):
    with open(path, "rb") as file:
        request = urllib.request.Request(
            "https://partner.example.com/upload",
            data=file.read(),
            headers={"Authorization": f"Bearer {os.environ['PARTNER_TOKEN']}"},
        )
    urllib.request.urlopen(request)

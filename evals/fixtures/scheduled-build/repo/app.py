import subprocess


def sync():
    try:
        subprocess.run(["dbt", "build"], check=True)
    except subprocess.CalledProcessError:
        subprocess.run(["dbt", "retry"], check=True)
    subprocess.run(["python", "publish.py"], check=True)

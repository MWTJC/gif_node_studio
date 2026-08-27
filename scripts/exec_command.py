import subprocess


def execCmd(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True, shell=True)
    output = result.stdout
    error = result.stderr
    return output, error
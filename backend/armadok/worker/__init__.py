"""The worker: a small program that runs on each machine and carries out the hub's requests.

It dials the hub (no port to open on the machine), authenticates over the encrypted channel,
and runs only operations from a closed list.
"""

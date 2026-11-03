"""Encrypted, authenticated channel between the hub and a worker.

Shared by both sides so they cannot drift apart. Pure functions and small classes, no
network or database code, which keeps the cryptography easy to review and to test.
"""

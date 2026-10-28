"""
fleet — BasicC2 adapted into a fleet tasking system.

A controller (the desk) holds persistent outbound-initiated
connections from agents on the fleet boxes, dispatches named,
allowlisted jobs, and proves completion afterwards from two signed
stitch ledgers. See DESIGN.md for the full record.
"""

__version__ = "0.1.0"

"""Keeping ASTRA running unattended: supervision, backups, health and alerts.

    python -m astra.ops.supervisor        # run everything, restart what stops
    python -m astra.ops.backup --now      # take a backup of the database now

None of this talks to the eSAKSHI portal. It watches the processes that do,
keeps copies of what they stored, and says when something needs a person.
"""

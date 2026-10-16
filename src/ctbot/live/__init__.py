"""Live Robinhood execution. Importing this package is blocked inside paper processes."""

from ctbot.execution.isolation import in_paper_process

if in_paper_process():
    raise ImportError("ctbot.live cannot be imported in a paper-trading process")

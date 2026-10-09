"""Marker target for the execv repro: writes its file FIRST, before any imports."""
open(r"C:\temp\wr_test\marker_sim1_done.txt", "w").write("execv into marker_sim1 succeeded")

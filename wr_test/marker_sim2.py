"""Marker target for the execv repro: writes its file FIRST, before any imports."""
open(r"C:\temp\wr_test\marker_sim2_done.txt", "w").write("execv into marker_sim2 succeeded")

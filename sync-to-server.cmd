@echo off
rem Windows 入口：本机 PATH 上的 bash 是坏掉的 WSL（execvpe(/bin/bash) failed），必须显式走 Git Bash。
rem 用法与 sync-to-server.sh 相同：在本机仓库根目录执行  sync-to-server
"%ProgramFiles%\Git\bin\bash.exe" -lc "source ~/.bash_profile && bash sync-to-server.sh" %*

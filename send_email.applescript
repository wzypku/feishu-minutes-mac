-- 用 Microsoft Outlook 构造一封邮件草稿（默认显示出来让你检查后再发）
-- 参数: subject  htmlBody  attachPath(可空)  autoSend(0/1)  then name1 addr1 name2 addr2 ...
on run argv
	set theSubject to item 1 of argv
	set theBody to item 2 of argv
	set attachPath to item 3 of argv
	set autoSend to (item 4 of argv) as integer

	tell application "Microsoft Outlook"
		set newMsg to make new outgoing message with properties {subject:theSubject, content:theBody}
		set i to 5
		repeat while i < (count of argv)
			set rName to item i of argv
			set rAddr to item (i + 1) of argv
			make new recipient at newMsg with properties {email address:{name:rName, address:rAddr}}
			set i to i + 2
		end repeat
		if attachPath is not "" then
			try
				make new attachment at newMsg with properties {file:(POSIX file attachPath)}
			end try
		end if
		if autoSend is 1 then
			send newMsg
			return "sent"
		else
			open newMsg
			activate
			return "drafted"
		end if
	end tell
end run

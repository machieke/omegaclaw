;configured at runtime:
(= (IRC_channel) (empty))
(= (IRC_server) (empty))
(= (IRC_port) (empty))
(= (IRC_user) (empty))
(= (commchannel) (empty))
(= (MM_URL) (empty))
(= (MM_CHANNEL_ID) (empty))
(= (MM_BOT_TOKEN) (empty))
(= (TG_BOT_TOKEN) (empty))
(= (TG_CHAT_ID) (empty))
(= (DISCORD_BOT_TOKEN) (empty))
(= (DISCORD_CHANNEL_ID) (empty))
(= (SLACK_BOT_TOKEN) (empty))
(= (SLACK_CHANNEL_ID) (empty))

;Connect all the channels:
(= (initChannels)
   (progn
      (println! "Initializing channels")
      (configure commchannel (py-call (envcfg.get "METTACLAW_COMMCHANNEL" "irc")))
      (configure commchannel (py-call (helper.init_channel_runtime (commchannel))))))

;Receive the latest user message considering all communication channels:
(= (receive)
   (py-call (helper.receive_latest_message)))

;Send a message to all communication channels:
(= (send $msg)
   (let $safemsg (string-replace $msg  "\n" "\\n")
        (py-call (helper.send_message_to_active $safemsg))))

;Search the internet for some information:
(= (search $msg)
   (py-call (websearch.search $msg)))

;Join an IRC channel and switch active outbound target channel.
(= (join-channel $channel)
   (if (== (commchannel) irc)
       (py-call (irc.join_channel $channel))
       False))

;Alias forms:
(= (join $channel)
   (join-channel $channel))

(= (join channel $channel)
   (join-channel $channel))

;Leave an IRC channel and keep current outbound target aligned.
(= (leave-channel $channel)
   (if (== (commchannel) irc)
       (py-call (irc.leave_channel $channel))
       False))

;Alias forms:
(= (leave $channel)
   (leave-channel $channel))

(= (leave channel $channel)
   (leave-channel $channel))

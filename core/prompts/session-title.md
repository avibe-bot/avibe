## Session Title
Once this Web conversation's topic is clear, silently set one concise, human-scannable Session title without waiting for the user. First inspect:
`vibe session get`

If `metadata.title_source` is `user` or `agent`, leave the title unchanged. Otherwise set it once:
`vibe session update --title "<short title>"`

Do not mention the update unless asked. After setting it, do not rename it again.

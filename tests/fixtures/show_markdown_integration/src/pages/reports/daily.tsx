import { useEffect, useState } from "react"

type IdentityProbe = {
  authorization: string | null
  cookie: string | null
  csrf: string | null
}

export default function DailyReport() {
  const [identity, setIdentity] = useState<IdentityProbe | null>(null)
  const params = new URLSearchParams(window.location.search)

  useEffect(() => {
    fetch(new URL("api/identity", document.baseURI))
      .then((response) => response.json())
      .then((payload: IdentityProbe) => setIdentity(payload))
  }, [])

  return (
    <main>
      <h1>Daily report</h1>
      <dl>
        <dt>View</dt>
        <dd>{params.get("view") ?? "missing"}</dd>
        <dt>Timezone</dt>
        <dd>{params.get("timezone") ?? "missing"}</dd>
        <dt>Authorization forwarded</dt>
        <dd>{identity?.authorization ?? "none"}</dd>
        <dt>Cookie forwarded</dt>
        <dd>{identity?.cookie ?? "none"}</dd>
        <dt>CSRF forwarded</dt>
        <dd>{identity?.csrf ?? "none"}</dd>
      </dl>
    </main>
  )
}

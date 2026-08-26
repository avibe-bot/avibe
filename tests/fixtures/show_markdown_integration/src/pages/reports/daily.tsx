import { useEffect, useState } from "react"

type IdentityProbe = {
  authorization: string | null
  cookie: string | null
  csrf: string | null
}

type IdentityState =
  | { status: "loading" }
  | { status: "ready"; value: IdentityProbe }
  | { status: "error"; message: string }

export default function DailyReport() {
  const [identity, setIdentity] = useState<IdentityState>({ status: "loading" })
  const params = new URLSearchParams(window.location.search)

  useEffect(() => {
    fetch(new URL("api/identity", document.baseURI))
      .then((response) => {
        if (!response.ok) throw new Error(`identity probe returned ${response.status}`)
        return response.json()
      })
      .then((payload: IdentityProbe) => setIdentity({ status: "ready", value: payload }))
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error)
        setIdentity({ status: "error", message })
      })
  }, [])

  const value = identity.status === "ready" ? identity.value : null

  return (
    <main>
      <h1>Daily report</h1>
      <dl>
        <dt>View</dt>
        <dd>{params.get("view") ?? "missing"}</dd>
        <dt>Timezone</dt>
        <dd>{params.get("timezone") ?? "missing"}</dd>
        <dt>Identity probe</dt>
        <dd>{identity.status === "error" ? `error: ${identity.message}` : identity.status}</dd>
        <dt>Authorization forwarded</dt>
        <dd>{value ? value.authorization ?? "none" : "pending"}</dd>
        <dt>Cookie forwarded</dt>
        <dd>{value ? value.cookie ?? "none" : "pending"}</dd>
        <dt>CSRF forwarded</dt>
        <dd>{value ? value.csrf ?? "none" : "pending"}</dd>
      </dl>
    </main>
  )
}

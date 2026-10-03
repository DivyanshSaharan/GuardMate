export async function request<T>(
  path: string,
  payload?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  let response: Response
  try {
    response = await fetch(`/api${path}`, {
      signal,
      method: payload === undefined ? 'GET' : 'PUT',
      headers:
        payload === undefined
          ? undefined
          : { 'Content-Type': 'application/json' },
      body: payload === undefined ? undefined : JSON.stringify(payload),
    })
  } catch {
    throw new Error(
      'Unable to reach GuardMate. Check that it is running and try again.',
    )
  }
  if (!response.ok) {
    if (response.status >= 500) {
      throw new Error(
        'GuardMate is temporarily unavailable. Check that the backend is running.',
      )
    }
    const body = await response.json().catch(() => ({}))
    const detail = body.detail
    if (typeof detail === 'string') throw new Error(detail)
    if (Array.isArray(detail)) {
      throw new Error(detail.map((item: { msg: string }) => item.msg).join(' '))
    }
    throw new Error('Your changes could not be saved. Please try again.')
  }
  return response.json() as Promise<T>
}

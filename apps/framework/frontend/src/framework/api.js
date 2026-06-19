export async function fetchJson(url, options) {
  const response = await fetch(url, options || {});
  const data = await response.json().catch(() => ({}));
  return { response, data };
}

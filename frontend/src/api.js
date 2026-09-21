// 后端接口封装。
//
// 客户端只用 job_id + 文件下标定位东西，从不接触服务端的文件名或路径。

async function asJson(res) {
  if (res.ok) return res.json()
  // FastAPI 用 detail 承载错误信息；拿不到就退回状态码。
  let detail = `请求失败（${res.status}）`
  try {
    const body = await res.json()
    if (typeof body.detail === 'string') detail = body.detail
    else if (Array.isArray(body.detail) && body.detail[0]?.msg) detail = body.detail[0].msg
  } catch {
    /* 响应体不是 JSON，用默认文案 */
  }
  throw new Error(detail)
}

export const getLimits = () => fetch('/api/limits').then(asJson)

// 用 XHR 而不是 fetch：上传进度对用户是刚需，fetch 拿不到。
function upload(url, files, onProgress) {
  const form = new FormData()
  for (const f of files) form.append('files', f, f.name)

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', url)
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total)
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText))
        } catch {
          reject(new Error('服务器返回了无法解析的响应。'))
        }
        return
      }
      let detail = `上传失败（${xhr.status}）`
      try {
        const body = JSON.parse(xhr.responseText)
        if (typeof body.detail === 'string') detail = body.detail
      } catch {
        /* 不是 JSON，用默认文案 */
      }
      reject(new Error(detail))
    }
    xhr.onerror = () => reject(new Error('网络中断，上传未完成。'))
    xhr.send(form)
  })
}

export const createJob = (files, onProgress) =>
  upload('/api/jobs', files, onProgress)

export const addFiles = (jobId, files, onProgress) =>
  upload(`/api/jobs/${jobId}/files`, files, onProgress)

export const getJob = (jobId) => fetch(`/api/jobs/${jobId}`).then(asJson)

export const startMerge = (jobId, body) =>
  fetch(`/api/jobs/${jobId}/merge`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(asJson)

export const removeFile = (jobId, fileId) =>
  fetch(`/api/jobs/${jobId}/files/${fileId}`, { method: 'DELETE' }).then(asJson)

export const thumbUrl = (jobId, fileId) =>
  `/api/jobs/${jobId}/files/${fileId}/thumb`

export const downloadUrl = (jobId) => `/api/jobs/${jobId}/download`

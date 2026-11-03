export interface Role {
  id: string
  name: string
  builtin: boolean
  rights: string[]
}

export interface Group {
  id: string
  name: string
  members: string[]
}

export interface Assignment {
  id: string
  subject_type: 'user' | 'group'
  subject_id: string
  subject_name: string | null
  role_id: string
  role_name: string
  role_builtin: number
  scope: string
  expires_at: string | null
}

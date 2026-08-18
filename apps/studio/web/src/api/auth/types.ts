export interface AuthUser {
  user_id: number
  username: string
  display_name: string
  roles: string[]
  disabled: boolean
}

export interface LoginRequest {
  username: string
  password: string
}

export interface LoginResponse {
  access_token: string
  token_type: string
  expires_in: number
  user: AuthUser
}

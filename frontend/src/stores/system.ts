import { defineStore } from 'pinia'
import { api } from '../api'
export const useSystemStore = defineStore('system', { state:()=>({online:false, version:'', checked:false}), actions:{ async refresh(){ try { const health=await api.health(); this.online=health.status==='ok'; this.version=health.version } catch { this.online=false } finally { this.checked=true } } } })

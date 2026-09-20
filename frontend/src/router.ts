import { createRouter, createWebHistory } from 'vue-router'
import DashboardView from './views/DashboardView.vue'
import SourceView from './views/SourceView.vue'
import ContentView from './views/ContentView.vue'
import JobsView from './views/JobsView.vue'
import PublicationView from './views/PublicationView.vue'
import AnalyticsView from './views/AnalyticsView.vue'
import SetupView from './views/SetupView.vue'
import StrategyView from './views/StrategyView.vue'
export default createRouter({ history:createWebHistory(), routes:[
  {path:'/', component:DashboardView, meta:{title:'总览'}}, {path:'/sources',component:SourceView,meta:{title:'选题库'}},
  {path:'/contents',component:ContentView,meta:{title:'内容工作台'}}, {path:'/jobs',component:JobsView,meta:{title:'任务中心'}},
  {path:'/publications',component:PublicationView,meta:{title:'发布与设备'}}, {path:'/analytics',component:AnalyticsView,meta:{title:'数据复盘'}},
  {path:'/strategy',component:StrategyView,meta:{title:'策略与实验'}},
  {path:'/setup',component:SetupView,meta:{title:'模板与设置'}}
] })

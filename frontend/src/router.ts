import {
  createRouter,
  createWebHistory,
  type RouterHistory,
} from "vue-router";

import AdminKnowledgeView from "./views/AdminKnowledgeView.vue";
import ContractReviewView from "./views/ContractReviewView.vue";


export function isAdmin(): boolean {
  return window.localStorage.getItem("actor_role") === "admin";
}

export function createAppRouter(history: RouterHistory = createWebHistory()) {
  const router = createRouter({
    history,
    routes: [
      { path: "/", component: ContractReviewView },
      {
        path: "/admin/knowledge",
        component: AdminKnowledgeView,
        meta: { requiresAdmin: true },
      },
    ],
  });

  router.beforeEach((to) => {
    if (Boolean(to.meta.requiresAdmin) && !isAdmin()) {
      return "/";
    }
    return true;
  });

  return router;
}

export const router = createAppRouter();

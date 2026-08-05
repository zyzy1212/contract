<script setup lang="ts">
import { ref } from "vue";
import { RouterLink, RouterView } from "vue-router";

const role = ref(
  window.localStorage.getItem("actor_role") === "admin" ? "admin" : "customer",
);

function switchRole() {
  if (role.value === "admin") {
    window.localStorage.setItem("actor_role", "admin");
    window.localStorage.setItem("actor_user", "admin-a");
    window.localStorage.setItem(
      "actor_tenant",
      "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    );
  } else {
    window.localStorage.setItem("actor_role", "customer");
    window.localStorage.setItem("actor_user", "user-a");
    window.localStorage.setItem(
      "actor_tenant",
      "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    );
  }
  window.location.reload();
}
</script>

<template>
  <div class="app-shell">
    <nav class="app-nav">
      <RouterLink to="/">合同审核</RouterLink>
      <RouterLink to="/admin/knowledge">知识库管理</RouterLink>
      <label class="role-switch">
        <span>测试角色</span>
        <select v-model="role" data-role-switch @change="switchRole">
          <option value="customer">客户</option>
          <option value="admin">管理员</option>
        </select>
      </label>
    </nav>
    <RouterView v-slot="{ Component }">
      <KeepAlive>
        <component :is="Component" />
      </KeepAlive>
    </RouterView>
  </div>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
}

.app-nav {
  display: flex;
  gap: 4px;
  padding: 10px 24px;
  background: #ffffff;
  border-bottom: 1px solid #d8dee9;
}

.app-nav a {
  padding: 6px 12px;
  border-radius: 8px;
  color: #475569;
  font-size: 14px;
  text-decoration: none;
}

.app-nav a.router-link-active {
  background: #f0f9ff;
  color: #0369a1;
  font-weight: 600;
}

.role-switch {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
  color: #475569;
  font-size: 13px;
}

.role-switch select {
  height: 32px;
  padding: 0 8px;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  background: #ffffff;
  color: #0f172a;
}
</style>

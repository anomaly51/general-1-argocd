# Текущий рабочий стол Windows в Hermes

Hermes в Kubernetes подключается через OpenSSH к пользователю, который уже
работает за ПК. Cua Driver открывает доступ к его интерактивному сеансу через MCP.
Codex CLI запускается по SSH в том же аккаунте. Панель **Windows PC** в Hermes
показывает свежие кадры основного монитора рядом с чатом. Просмотр включается
вручную и обновляется примерно раз в секунду. Стандартный Bot Screen показывает
отдельный рабочий стол Linux.
[Cua Driver по SSH](https://cua.ai/docs/cua-driver/guides/vms-and-remote)
[Bot Screen](https://hermes-agent.nousresearch.com/docs/user-guide/features/bot-screen)

## Подготовка Windows

1. Установи Cua Driver и Codex CLI. В PowerShell под целевым пользователем
   выполни `codex login`, если `codex login status` показывает, что вход не
   выполнен. [Cua Driver](https://cua.ai/docs/cua-driver/quickstart)
   [Codex CLI](https://learn.chatgpt.com/docs/cli)
2. В текущем интерактивном сеансе выполни `.\Setup.ps1 -EnableDesktop`.
   Скрипт регистрирует автозапуск Cua Driver для этого пользователя и
   выводит LAN IPv4, SID и состояние SSH. Без параметра скрипт только проверяет.
3. Установи и запусти OpenSSH Server. Ограничь входящий TCP 22 адресами,
   с которых узлы кластера выходят в LAN. Добавь отдельный публичный ключ
   Hermes для входа под тем же Windows-пользователем. Для члена Administrators
   ограничь ключ этим аккаунтом отдельным правилом `Match User` и его файлом
   ключей, если OpenSSH использует общий `administrators_authorized_keys`.
   [OpenSSH Server](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse)
   [Управление ключами](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_keymanagement)
4. Прочитай публичный ключ хоста `C:\ProgramData\ssh\ssh_host_ed25519_key.pub`
   прямо на Windows и сверь его отпечаток. Передай оператору кластера LAN IP,
   имя SSH-пользователя, публичный ключ хоста и отпечаток. По SSH проверь
   `whoami /user`, `where.exe cua-driver.exe`, `where.exe codex` и
   `cua-driver call list_apps`. SID должен совпасть с интерактивным сеансом.

## Подключение к кластеру

Создай отдельную ключевую пару для Hermes. На Windows нужен только публичный
ключ. Возьми [шаблон SSH](ssh.config.example), подставь LAN IP и пользователя.
В `known_hosts` запиши `hermes-windows-pc ssh-ed25519 <публичный ключ хоста>`.
Создай файл логина для владельца через `htpasswd -B -c htpasswd owner`.
При включении Windows PC существующий HTTPS-интерфейс Hermes целиком будет
запрашивать этот логин. Telegram использует существующий список разрешённых
пользователей.

Запиши четыре поля в Vault и дождись синхронизации VSO. Аргументы с `@`
читают локальные файлы. Храни реальные адреса, учётные данные и ключи вне Git.

```sh
vault kv patch kv/apps/hermes \
  WINDOWS_PC_SSH_CONFIG=@ssh.config \
  WINDOWS_PC_SSH_KEY=@id_ed25519 \
  WINDOWS_PC_KNOWN_HOSTS=@known_hosts \
  WINDOWS_PC_WEB_HTPASSWD=@htpasswd
```

Проверь доступ к SSH из сети кластера. Установи `windowsPc.enabled: true`
в Helm values, запушь изменение и проверь Argo CD. Открой HTTPS-интерфейс,
войди и нажми **Windows PC → Смотреть экран**. Попроси Hermes через MCP
открыть Калькулятор и проверь, что окно видно на реальном экране и в панели.
Затем попроси Windows Codex запустить задачу на чтение в каталоге проекта.
До обеих проверок подключение считается неподтверждённым.

Просмотр работает, пока доступен интерактивный сеанс Windows. Панель показывает
основной монитор без записи кадров. Включай её, когда люди за этим ПК знают,
что экран будет виден удалённо.

Чтобы отключить подключение, запушь `windowsPc.enabled: false`, проверь
Argo CD и отзови ключ Hermes на Windows. Автозапуск драйвера отключается
командой `cua-driver autostart disable` из сеанса Windows.

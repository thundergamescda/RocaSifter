#include <ctype.h>
#include <errno.h>
#include <inttypes.h>
#include <linux/capability.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/utsname.h>
#include <sys/xattr.h>
#include <unistd.h>

#ifndef SS_SOURCE_SHA256
#error SS_SOURCE_SHA256 must be supplied by the fixed reviewed compile command
#endif

struct extent { unsigned long long inside, outside, length; };
struct context {
  unsigned long long uid[4], gid[4];
  unsigned long long caps[5]; /* inheritable, permitted, effective, bounding, ambient */
  unsigned int nnp, seccomp, filters;
  gid_t groups[16]; int group_count;
  struct extent uid_map[4], gid_map[4]; int uid_count, gid_count;
  char user_ns[96], pid_ns[96], net_ns[96], lsm[256], setgroups[32];
  struct utsname kernel;
  struct stat executable, root;
};
static const char *problem = "unknown_setup_failure";
static int setup_errno;
#define REQUIRE(test, name) do { if (!(test)) { problem=(name); setup_errno=errno; return false; } } while (0)

static void string_json(const char *value) {
  putchar('"');
  for (const unsigned char *p=(const unsigned char *)value; *p; ++p) {
    if (*p=='"' || *p=='\\') { putchar('\\'); putchar(*p); }
    else if (*p<32 || *p>126) printf("\\u%04x", (unsigned int)*p);
    else putchar(*p);
  }
  putchar('"');
}
static bool text_file(const char *path, char *output, size_t size) {
  FILE *file=fopen(path, "r");
  REQUIRE(file!=NULL, "metadata_open_failed");
  size_t count=fread(output, 1, size-1, file);
  bool okay=!ferror(file) && feof(file);
  int closed=fclose(file);
  REQUIRE(okay && closed==0, "metadata_read_or_bound_failed");
  output[count]='\0';
  while (count && isspace((unsigned char)output[count-1])) output[--count]='\0';
  return true;
}
static bool namespace_link(const char *path, char *output, size_t size) {
  ssize_t count=readlink(path, output, size-1);
  REQUIRE(count>0 && (size_t)count<size-1, "namespace_link_failed");
  output[count]='\0'; return true;
}
static bool namespace_name(const char *name) {
  if (strncmp(name,"user:[",6)!=0) return false;
  const char *p=name+6;
  if (!isdigit((unsigned char)*p)) return false;
  for (; isdigit((unsigned char)*p); ++p) {}
  return p[0]==']' && p[1]=='\0';
}
static bool maps(const char *path, struct extent *rows, int *count) {
  FILE *file=fopen(path,"r");
  REQUIRE(file!=NULL,"map_open_failed");
  char line[160]; bool okay=true; *count=0;
  while (fgets(line,sizeof(line),file)) {
    if (*count>=4 || !strchr(line,'\n')) { okay=false; break; }
    int end=0;
    struct extent row;
    if (sscanf(line,"%llu %llu %llu %n",&row.inside,&row.outside,&row.length,&end)!=3 ||
        row.inside>UINT32_MAX || row.outside>UINT32_MAX || !row.length || row.length>UINT32_MAX) { okay=false; break; }
    for (char *p=line+end; *p; ++p) if (!isspace((unsigned char)*p)) okay=false;
    if (!okay) break;
    rows[(*count)++]=row;
  }
  okay=okay && !ferror(file) && feof(file) && *count>0;
  int closed=fclose(file);
  REQUIRE(okay && closed==0,"map_shape_or_bound_failed"); return true;
}
static bool status(struct context *c) {
  FILE *file=fopen("/proc/self/status","r");
  REQUIRE(file!=NULL,"status_open_failed");
  char line[1024]; unsigned int seen=0; bool okay=true;
  const char *names[5]={"CapInh:","CapPrm:","CapEff:","CapBnd:","CapAmb:"};
  while (fgets(line,sizeof(line),file)) {
    if (!strchr(line,'\n')) { okay=false; break; }
    if (strncmp(line,"Uid:",4)==0) {
      if ((seen&1) || sscanf(line+4,"%llu %llu %llu %llu",&c->uid[0],&c->uid[1],&c->uid[2],&c->uid[3])!=4) { okay=false; break; }
      seen|=1;
    } else if (strncmp(line,"Gid:",4)==0) {
      if ((seen&2) || sscanf(line+4,"%llu %llu %llu %llu",&c->gid[0],&c->gid[1],&c->gid[2],&c->gid[3])!=4) { okay=false; break; }
      seen|=2;
    } else if (strncmp(line,"NoNewPrivs:",11)==0) {
      if ((seen&4) || sscanf(line+11,"%u",&c->nnp)!=1) { okay=false; break; } seen|=4;
    } else if (strncmp(line,"Seccomp:",8)==0) {
      if ((seen&8) || sscanf(line+8,"%u",&c->seccomp)!=1) { okay=false; break; } seen|=8;
    } else if (strncmp(line,"Seccomp_filters:",16)==0) {
      if ((seen&16) || sscanf(line+16,"%u",&c->filters)!=1) { okay=false; break; } seen|=16;
    } else for (int i=0;i<5;++i) if (strncmp(line,names[i],7)==0) {
      unsigned int bit=1U<<(5+i);
      if ((seen&bit) || sscanf(line+7,"%llx",&c->caps[i])!=1) { okay=false; break; }
      seen|=bit;
    }
    if (!okay) break;
  }
  okay=okay && !ferror(file) && feof(file) && seen==1023;
  int closed=fclose(file);
  REQUIRE(okay && closed==0,"status_shape_or_bound_failed");
  return true;
}
static bool collect(struct context *c, bool inner, const char *parent_ns) {
  REQUIRE(status(c),"status_collection_failed");
  uid_t r,e,s; gid_t gr,ge,gs;
  REQUIRE(getresuid(&r,&e,&s)==0 && getresgid(&gr,&ge,&gs)==0,"res_ids_failed");
  REQUIRE(c->uid[0]==r && c->uid[1]==e && c->uid[2]==s &&
          c->gid[0]==gr && c->gid[1]==ge && c->gid[2]==gs,"res_ids_disagree");
  unsigned long long wanted=inner?0:10001;
  for (int i=0;i<4;++i) REQUIRE(c->uid[i]==wanted && c->gid[i]==wanted,"execution_ids_mismatch");
  REQUIRE(c->nnp==1 && c->seccomp==2 && c->filters>=1,"process_security_state_mismatch");
  REQUIRE(prctl(PR_GET_NO_NEW_PRIVS,0,0,0,0)==1 && prctl(PR_GET_SECCOMP,0,0,0,0)==2,"prctl_security_state_mismatch");
  struct __user_cap_header_struct header={_LINUX_CAPABILITY_VERSION_3,0};
  struct __user_cap_data_struct data[2]={{0}};
  REQUIRE(syscall(SYS_capget,&header,data)==0,"capget_failed");
  REQUIRE(c->caps[0]==((uint64_t)data[0].inheritable|((uint64_t)data[1].inheritable<<32)) &&
          c->caps[1]==((uint64_t)data[0].permitted|((uint64_t)data[1].permitted<<32)) &&
          c->caps[2]==((uint64_t)data[0].effective|((uint64_t)data[1].effective<<32)),"capget_disagrees");
  if (inner) {
    REQUIRE((c->caps[1]&(1ULL<<CAP_SYS_CHROOT)) && (c->caps[2]&(1ULL<<CAP_SYS_CHROOT)) &&
            (c->caps[3]&(1ULL<<CAP_SYS_CHROOT)),"namespace_chroot_capability_missing");
  } else for (int i=0;i<5;++i) REQUIRE(c->caps[i]==0,"outer_capability_nonzero");
  c->group_count=getgroups(16,c->groups);
  REQUIRE(c->group_count>=0 && c->group_count<=16,"supplementary_group_bound_failed");
  REQUIRE(maps("/proc/self/uid_map",c->uid_map,&c->uid_count) &&
          maps("/proc/self/gid_map",c->gid_map,&c->gid_count),"map_collection_failed");
  REQUIRE(text_file("/proc/self/setgroups",c->setgroups,sizeof(c->setgroups)),"setgroups_read_failed");
  if (inner) {
    REQUIRE(c->uid_count==1 && c->gid_count==1 && c->uid_map[0].inside==0 &&
      c->uid_map[0].outside==10001 && c->uid_map[0].length==1 && c->gid_map[0].inside==0 &&
      c->gid_map[0].outside==10001 && c->gid_map[0].length==1 && strcmp(c->setgroups,"deny")==0,"inner_exact_mapping_mismatch");
  }
  REQUIRE(namespace_link("/proc/self/ns/user",c->user_ns,sizeof(c->user_ns)) &&
          namespace_link("/proc/self/ns/pid",c->pid_ns,sizeof(c->pid_ns)) &&
          namespace_link("/proc/self/ns/net",c->net_ns,sizeof(c->net_ns)),"namespace_identity_failed");
  REQUIRE(namespace_name(c->user_ns) && ((strcmp(c->user_ns,parent_ns)!=0)==inner),"parent_namespace_mismatch");
  REQUIRE(text_file("/proc/self/attr/current",c->lsm,sizeof(c->lsm)),"lsm_metadata_unavailable");
  REQUIRE(uname(&c->kernel)==0 && strcmp(c->kernel.sysname,"Linux")==0 && strcmp(c->kernel.machine,"x86_64")==0,"kernel_architecture_mismatch");
  REQUIRE(stat("/proc/self/exe",&c->executable)==0 && S_ISREG(c->executable.st_mode) &&
          !(c->executable.st_mode&(S_ISUID|S_ISGID)),"executable_metadata_rejected");
  errno=0; char capability[64];
  ssize_t cap_size=getxattr("/proc/self/exe","security.capability",capability,sizeof(capability));
  REQUIRE(cap_size==-1 && errno==ENODATA,"executable_file_capabilities_unconfirmed");
  REQUIRE(stat("/",&c->root)==0 && S_ISDIR(c->root.st_mode),"root_target_metadata_failed");
  return true;
}
static void map_json(const struct extent *rows,int count) {
  putchar('[');
  for (int i=0;i<count;++i) { if(i) putchar(','); printf("[%llu,%llu,%llu]",rows[i].inside,rows[i].outside,rows[i].length); }
  putchar(']');
}
static void context_json(const struct context *c) {
  printf("\"uid\":[%llu,%llu,%llu,%llu],\"gid\":[%llu,%llu,%llu,%llu],",c->uid[0],c->uid[1],c->uid[2],c->uid[3],c->gid[0],c->gid[1],c->gid[2],c->gid[3]);
  printf("\"caps\":{\"inheritable\":\"%016llx\",\"permitted\":\"%016llx\",\"effective\":\"%016llx\",\"bounding\":\"%016llx\",\"ambient\":\"%016llx\"},",c->caps[0],c->caps[1],c->caps[2],c->caps[3],c->caps[4]);
  printf("\"effective_chroot_capability\":%s,\"nnp\":%u,\"seccomp\":%u,\"seccomp_filters\":%u,",(c->caps[2]&(1ULL<<CAP_SYS_CHROOT))?"true":"false",c->nnp,c->seccomp,c->filters);
  printf("\"supplementary_groups\":[");
  for(int i=0;i<c->group_count;++i) { if(i) putchar(','); printf("%ju",(uintmax_t)c->groups[i]); }
  printf("],\"uid_map\":"); map_json(c->uid_map,c->uid_count);
  printf(",\"gid_map\":"); map_json(c->gid_map,c->gid_count);
  printf(",\"setgroups\":"); string_json(c->setgroups);
  printf(",\"user_ns\":"); string_json(c->user_ns);
  printf(",\"pid_ns\":"); string_json(c->pid_ns);
  printf(",\"net_ns\":"); string_json(c->net_ns);
  printf(",\"lsm\":"); string_json(c->lsm);
  printf(",\"kernel_release\":"); string_json(c->kernel.release);
  printf(",\"machine\":"); string_json(c->kernel.machine);
  printf(",\"executable\":{\"bytes\":%ju,\"mode\":%u,\"uid\":%ju,\"gid\":%ju,\"device\":%ju,\"inode\":%ju,\"file_capabilities_absent\":true},",(uintmax_t)c->executable.st_size,(unsigned int)c->executable.st_mode,(uintmax_t)c->executable.st_uid,(uintmax_t)c->executable.st_gid,(uintmax_t)c->executable.st_dev,(uintmax_t)c->executable.st_ino);
  printf("\"root_target\":{\"device\":%ju,\"inode\":%ju,\"mode\":%u}",(uintmax_t)c->root.st_dev,(uintmax_t)c->root.st_ino,(unsigned int)c->root.st_mode);
}
int main(int argc,char **argv) {
  if(argc!=4 || (strcmp(argv[1],"original") && strcmp(argv[1],"experimental")) ||
     (strcmp(argv[2],"outer") && strcmp(argv[2],"inner")) || !namespace_name(argv[3])) return 78;
  bool inner=strcmp(argv[2],"inner")==0;
  const char *id=strcmp(argv[1],"original")==0?(inner?"CHROOT02":"CHROOT01"):(inner?"CHROOT04":"CHROOT03");
  struct context c={0}; errno=0;
  if(!collect(&c,inner,argv[3])) {
    printf("{\"schema\":1,\"case_id\":\"%s\",\"profile\":\"%s\",\"mode\":\"%s\",\"context_valid\":false,\"stage\":\"setup_error\",\"chroot_attempted\":false,\"setup_code\":\"%s\",\"setup_errno\":%d,\"chroot_return\":null,\"chroot_errno\":null}\n",id,argv[1],argv[2],problem,setup_errno);
    return fflush(stdout)==0?78:79;
  }
  /* Single process, same credentials/context. No exec between collection and
     this fixed direct call, and errno is saved before any logging/function. */
  errno=0;
  int result=chroot("/");
  int saved_errno=result==0?0:errno;
  printf("{\"schema\":1,\"case_id\":\"%s\",\"profile\":\"%s\",\"mode\":\"%s\",\"source_sha256\":\"%s\",\"context_valid\":true,\"stage\":\"chroot_complete\",\"chroot_attempted\":true,\"target\":\"/\",\"chroot_return\":%d,\"chroot_errno\":%d,\"parent_user_ns\":",id,argv[1],argv[2],SS_SOURCE_SHA256,result,saved_errno);
  string_json(argv[3]); putchar(','); context_json(&c); printf("}\n");
  /* No exec, chdir or further filesystem observation follows chroot. */
  return fflush(stdout)==0?0:79;
}

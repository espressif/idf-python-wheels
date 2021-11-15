use s3::bucket::Bucket;
use s3::creds::Credentials;

use multimap::MultiMap;
use std::env;
use regex::Regex;

use indicatif::ProgressBar;


async fn upload_wheels(bucket: &Bucket, dl_prefix: &String) -> Result<(), Box<dyn std::error::Error>> {
    let pb = ProgressBar::new(25);

    let path = "./download/";
    let mut files_in_dir = tokio::fs::read_dir(&path).await?;

    while let Some(entry) = files_in_dir.next_entry().await? {
        let name = entry.file_name().into_string().unwrap();
        if !name.contains(".whl") {
            continue;
        }

        let re = Regex::new(r"^(.*?)-").unwrap(); //([a-zA-Z_\d]+)?\-.+
        let file = tokio::fs::read(entry.path()).await?;
        let prefix = re.captures(&name).unwrap().get(1).unwrap().as_str().to_lowercase();

        bucket.put_object_with_content_type( dl_prefix.to_owned() + "/" + &prefix + "/" + &name, &file, "binary/octet-stream").await?;
        pb.inc(1);
    }

    pb.finish_with_message("Upload done");

    Ok(())
}

async fn create_indexes(bucket: &Bucket, dl_prefix: &String) -> Result<(), Box<dyn std::error::Error>> {
    let header = r#"
<!DOCTYPE html>
<html>
    <head>
        <meta name="pypi:repository-version" content="1.0">
        <title>Simple index</title>
    </head>
<body>"#;

    let footer = r#"
</body>
</html>"#;

    let result_list = bucket.list(dl_prefix.to_string(), None).await?;
    let mut packages = MultiMap::new();
    let pb = ProgressBar::new((2*result_list.len()).try_into().unwrap());
    for file in result_list {
        for item in file.contents {
            if !item.key.contains(".whl") {
                continue;
            }
            let re = Regex::new(r"([a-zA-Z_\d]+)\-.+").unwrap();
            let m = re.captures(&item.key).unwrap();
            let name = m.get(1).unwrap().as_str().to_lowercase();
            let path = m.get(0).unwrap().as_str();
            packages.insert(name, path.to_string());
            pb.inc(1);
        }
    }
    
    let mut index = String::new();
    index.push_str(header);

    let pb = ProgressBar::new((packages.len()).try_into().unwrap());

    for name in packages.keys() {
        index.push_str(&format!("\n<a href=\"/{prefix}/{item}/\">{item}/</a>", prefix=dl_prefix, item=name));
        pb.inc(1);
    }
    index.push_str(&footer);

    bucket.put_object_with_content_type( dl_prefix.to_owned() + "/" + "index.html", &index.into_bytes(), "text/html").await?;

    for (key, files) in packages {

        let mut index = String::new();
        index.push_str(header);

        for file in files {
            index.push_str(&format!("\n<a href=\"/{prefix}/{item}/{filename}\">{filename}</a>", prefix=dl_prefix, item=key, filename=file));
            
        }
        index.push_str(&footer);
        bucket.put_object_with_content_type( format!("{prefix}/{name}/index.html", prefix=dl_prefix, name=key), &index.into_bytes(), "text/html").await?;
        pb.inc(1);
    }
    
    pb.finish_with_message("Indexes created");

    Ok(())
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    
    let dl_bucket = env::var("AWS_BUCKET").unwrap_or("".to_string());
    let prefix = env::var("PREFIX").unwrap_or("test".to_string());
    let aws_access_key = env::var("AWS_ACCESS_KEY_ID").unwrap_or("".to_string());
    let aws_secret_key = env::var("AWS_SECRET_ACCESS_KEY").unwrap_or("".to_string());
    let aws_region = env::var("AWS_DEFAULT_REGION").unwrap_or("eu-west-1".to_string()).parse()?;

    assert_ne!(dl_bucket, "");
    assert_ne!(aws_access_key, "");
    assert_ne!(aws_secret_key, "");
    //assert_ne!(AWS_DEFAULT_REGION, "");
    
    let credentials = Credentials::new(
        Some(&aws_access_key[..]),
        Some(&aws_secret_key[..]),
     None, None, None)?;

    let mut bucket = Bucket::new(
        &dl_bucket[..], 
        aws_region, 
        credentials)?;
    bucket.add_header("x-amz-acl", "public-read");
    
    upload_wheels(&bucket, &prefix).await?;
    create_indexes(&bucket, &prefix).await?;
    
    Ok(())
}
